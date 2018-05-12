from warnings import warn

from django.conf import settings
from django.contrib.postgres.search import SearchQuery as PostgresSearchQuery
from django.contrib.postgres.search import SearchRank, SearchVector
from django.db import DEFAULT_DB_ALIAS, NotSupportedError, connections, transaction
from django.db.models import F, Manager, Q, TextField, Value
from django.db.models.constants import LOOKUP_SEP
from django.db.models.functions import Cast
from django.utils.encoding import force_text

from wagtail.search.backends.base import (
    BaseSearchBackend, BaseSearchQueryCompiler, BaseSearchResults)
from wagtail.search.index import RelatedFields, SearchField, get_indexed_models
from wagtail.search.query import And, MatchAll, Not, Or, Prefix, SearchQueryShortcut, Term
from wagtail.search.utils import ADD, AND, OR

from .models import SearchAutocomplete as PostgresSearchAutocomplete
from .models import IndexEntry
from .utils import (
    get_content_type_pk, get_descendants_content_types_pks, get_postgresql_connections,
    get_sql_weights, get_weight, unidecode)


EMPTY_VECTOR = SearchVector(Value(''))


class Index:
    def __init__(self, backend, db_alias=None):
        self.backend = backend
        self.name = self.backend.index_name
        self.db_alias = DEFAULT_DB_ALIAS if db_alias is None else db_alias
        self.connection = connections[self.db_alias]
        if self.connection.vendor != 'postgresql':
            raise NotSupportedError(
                'You must select a PostgreSQL database '
                'to use PostgreSQL search.')
        self.entries = IndexEntry._default_manager.using(self.db_alias)

    def add_model(self, model):
        pass

    def refresh(self):
        pass

    def delete_stale_model_entries(self, model):
        existing_pks = (model._default_manager.using(self.db_alias)
                        .annotate(object_id=Cast('pk', TextField()))
                        .values('object_id'))
        content_types_pks = get_descendants_content_types_pks(model)
        valid_index_names = {
            params.get('INDEX', 'default')
            for params in settings.WAGTAILSEARCH_BACKENDS.values()}
        self.entries.filter(
            ~Q(index_name__in=valid_index_names)
            | (Q(index_name=self.name, content_type_id__in=content_types_pks)
               & ~Q(object_id__in=existing_pks))
        ).delete()

    def delete_stale_entries(self):
        for model in get_indexed_models():
            # We don’t need to delete stale entries for non-root models,
            # since we already delete them by deleting roots.
            if not model._meta.parents:
                self.delete_stale_model_entries(model)

    def prepare_value(self, value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return ', '.join(self.prepare_value(item) for item in value)
        if isinstance(value, dict):
            return ', '.join(self.prepare_value(item)
                             for item in value.values())
        return force_text(value)

    def prepare_field(self, obj, field):
        if isinstance(field, SearchField):
            yield (field, get_weight(field.boost),
                   unidecode(self.prepare_value(field.get_value(obj))))
        elif isinstance(field, RelatedFields):
            sub_obj = field.get_value(obj)
            if sub_obj is None:
                return
            if isinstance(sub_obj, Manager):
                sub_objs = sub_obj.all()
            else:
                if callable(sub_obj):
                    sub_obj = sub_obj()
                sub_objs = [sub_obj]
            for sub_obj in sub_objs:
                for sub_field in field.fields:
                    yield from self.prepare_field(sub_obj, sub_field)

    def prepare_obj(self, obj, search_fields):
        obj._object_id_ = force_text(obj.pk)
        obj._boost_ = obj.get_search_boost()
        obj._autocomplete_ = []
        obj._body_ = []
        for field in search_fields:
            for current_field, boost, value in self.prepare_field(obj, field):
                if isinstance(current_field, SearchField) and \
                        current_field.partial_match:
                    obj._autocomplete_.append((value, boost))
                else:
                    obj._body_.append((value, boost))

    def add_item(self, obj):
        self.add_items(obj._meta.model, [obj])

    def add_items_upsert(self, content_type_pk, objs):
        config = self.backend.config
        autocomplete_sql = []
        body_sql = []
        data_params = []
        sql_template = ('to_tsvector(%s)' if config is None
                        else "to_tsvector('%s', %%s)" % config)
        sql_template = 'setweight(%s, %%s)' % sql_template
        for obj in objs:
            data_params.extend((self.name, content_type_pk,
                                obj._object_id_, obj._boost_))
            if obj._autocomplete_:
                autocomplete_sql.append('||'.join(sql_template
                                                  for _ in obj._autocomplete_))
                data_params.extend([v for t in obj._autocomplete_ for v in t])
            else:
                autocomplete_sql.append("''::tsvector")
            if obj._body_:
                body_sql.append('||'.join(sql_template for _ in obj._body_))
                data_params.extend([v for t in obj._body_ for v in t])
            else:
                body_sql.append("''::tsvector")
        data_sql = ', '.join(['(%%s, %%s, %%s, %%s, %s, %s)' % (a, b)
                              for a, b in zip(autocomplete_sql, body_sql)])
        with self.connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO %s (index_name, content_type_id, object_id, boost,
                                autocomplete, body)
                (VALUES %s)
                ON CONFLICT (index_name, content_type_id, object_id)
                DO UPDATE SET boost = EXCLUDED.boost,
                              autocomplete = EXCLUDED.autocomplete,
                              body = EXCLUDED.body
                """ % (IndexEntry._meta.db_table, data_sql), data_params)

    def add_items_update_then_create(self, content_type_pk, objs):
        config = self.backend.config
        ids_and_objs = {}
        for obj in objs:
            obj._autocomplete_ = (
                ADD([SearchVector(Value(text), weight=weight, config=config)
                     for text, weight in obj._autocomplete_])
                if obj._autocomplete_ else EMPTY_VECTOR)
            obj._body_ = (
                ADD([SearchVector(Value(text), weight=weight, config=config)
                     for text, weight in obj._body_])
                if obj._body_ else EMPTY_VECTOR)
            ids_and_objs[obj._object_id_] = obj
        index_entries_for_ct = self.entries.filter(
            index_name=self.name, content_type_id=content_type_pk)
        indexed_ids = frozenset(
            index_entries_for_ct.filter(object_id__in=ids_and_objs)
            .values_list('object_id', flat=True))
        for indexed_id in indexed_ids:
            obj = ids_and_objs[indexed_id]
            index_entries_for_ct.filter(object_id=obj._object_id_) \
                .update(boost=obj._boost_,
                        autocomplete=obj._autocomplete_, body=obj._body_)
        to_be_created = []
        for object_id in ids_and_objs:
            if object_id not in indexed_ids:
                obj = ids_and_objs[object_id]
                to_be_created.append(IndexEntry(
                    index_name=self.name,
                    content_type_id=content_type_pk, object_id=object_id,
                    boost=obj._boost_,
                    autocomplete=obj._autocomplete_, body=obj._body_))
        self.entries.bulk_create(to_be_created)

    def add_items(self, model, objs):
        search_fields = model.get_search_fields()
        if not search_fields:
            return
        for obj in objs:
            self.prepare_obj(obj, search_fields)

        # TODO: Delete unindexed objects while dealing with proxy models.
        if objs:
            content_type_pk = get_content_type_pk(model)
            # Use a faster method for PostgreSQL >= 9.5
            update_method = (
                self.add_items_upsert if self.connection.pg_version >= 90500
                else self.add_items_update_then_create)
            update_method(content_type_pk, objs)

    def delete_item(self, item):
        item.index_entries.using(self.db_alias).delete()

    def __str__(self):
        return self.name


class PostgresSearchQueryCompiler(BaseSearchQueryCompiler):
    DEFAULT_OPERATOR = 'and'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.search_fields = self.queryset.model.get_searchable_search_fields()
        # Due to a Django bug, arrays are not automatically converted
        # when we use WEIGHTS_VALUES.
        self.sql_weights = get_sql_weights()
        # TODO: Better handle mixed queries containing
        #       both autocomplete and search.
        self.is_autocomplete = False
        if self.fields is not None:
            search_fields = self.queryset.model.get_searchable_search_fields()
            self.search_fields = {
                field_lookup: self.get_search_field(field_lookup,
                                                    fields=search_fields)
                for field_lookup in self.fields}

    def get_search_field(self, field_lookup, fields=None):
        if fields is None:
            fields = self.search_fields
        if LOOKUP_SEP in field_lookup:
            field_lookup, sub_field_name = field_lookup.split(LOOKUP_SEP, 1)
        else:
            sub_field_name = None
        for field in fields:
            if isinstance(field, SearchField) \
                    and field.field_name == field_lookup:
                return field
            # Note: Searching on a specific related field using
            # `.search(fields=…)` is not yet supported by Wagtail.
            # This method anticipates by already implementing it.
            if isinstance(field, RelatedFields) \
                    and field.field_name == field_lookup:
                return self.get_search_field(sub_field_name, field.fields)

    # TODO: Find a way to use the term boosting.
    def check_boost(self, query):
        if query.boost != 1:
            warn('PostgreSQL search backend '
                 'does not support term boosting for now.')

    def build_database_query(self, query=None):
        if query is None:
            query = self.query

        if isinstance(query, SearchQueryShortcut):
            return self.build_database_query(query.get_equivalent())
        if isinstance(query, Prefix):
            self.check_boost(query)
            self.is_autocomplete = True
            return PostgresSearchAutocomplete(unidecode(query.prefix),
                                              config=self.backend.config)
        if isinstance(query, Term):
            self.check_boost(query)
            return PostgresSearchQuery(unidecode(query.term),
                                       config=self.backend.config)
        if isinstance(query, Not):
            return ~self.build_database_query(query.subquery)
        if isinstance(query, And):
            return AND(self.build_database_query(subquery)
                       for subquery in query.subqueries)
        if isinstance(query, Or):
            return OR(self.build_database_query(subquery)
                      for subquery in query.subqueries)
        raise NotImplementedError(
            '`%s` is not supported by the PostgreSQL search backend.'
            % self.query.__class__.__name__)

    def search(self, start, stop, score_field=None):
        # TODO: Handle MatchAll nested inside other search query classes.
        if isinstance(self.query, MatchAll):
            return self.queryset[start:stop]

        queryset = self.queryset
        search_query = self.build_database_query()
        if self.fields is None:
            queryset = queryset.filter(
                index_entries__index_name=self.backend.index_name)
            vector = F('index_entries__autocomplete')
            if not self.is_autocomplete:
                vector = vector._combine(F('index_entries__body'), '||', False)
        else:
            vector = ADD(
                SearchVector(field_lookup, config=search_query.config,
                             weight=get_weight(search_field.boost))
                for field_lookup, search_field in self.search_fields.items()
                if not self.is_autocomplete or search_field.partial_match)
        rank_expression = SearchRank(vector, search_query,
                                     weights=self.sql_weights)
        queryset = queryset.annotate(
            _vector_=vector).filter(_vector_=search_query)
        if self.order_by_relevance:
            rank_expression *= F('index_entries__boost')
            queryset = queryset.order_by(rank_expression.desc(), '-pk')
        elif not queryset.query.order_by:
            # Adds a default ordering to avoid issue #3729.
            queryset = queryset.order_by('-pk')
            rank_expression = F('pk')
        if score_field is not None:
            queryset = queryset.annotate(**{score_field: rank_expression})
        return queryset[start:stop]

    def _process_lookup(self, field, lookup, value):
        return Q(**{field.get_attname(self.queryset.model) +
                    '__' + lookup: value})

    def _connect_filters(self, filters, connector, negated):
        if connector == 'AND':
            q = Q(*filters)
        elif connector == 'OR':
            q = OR([Q(fil) for fil in filters])
        else:
            return

        if negated:
            q = ~q

        return q


class PostgresSearchResults(BaseSearchResults):
    def _do_search(self):
        return list(self.query_compiler.search(
            self.start, self.stop, score_field=self._score_field))

    def _do_count(self):
        return self.query_compiler.search(
            None, None, score_field=self._score_field).count()


class PostgresSearchRebuilder:
    def __init__(self, index):
        self.index = index

    def start(self):
        self.index.delete_stale_entries()
        return self.index

    def finish(self):
        pass


class PostgresSearchAtomicRebuilder(PostgresSearchRebuilder):
    def __init__(self, index):
        super().__init__(index)
        self.transaction = transaction.atomic(using=index.db_alias)
        self.transaction_opened = False

    def start(self):
        self.transaction.__enter__()
        self.transaction_opened = True
        return super().start()

    def finish(self):
        self.transaction.__exit__(None, None, None)
        self.transaction_opened = False

    def __del__(self):
        # TODO: Implement a cleaner way to close the connection on failure.
        if self.transaction_opened:
            self.transaction.needs_rollback = True
            self.finish()


class PostgresSearchBackend(BaseSearchBackend):
    query_compiler_class = PostgresSearchQueryCompiler
    results_class = PostgresSearchResults
    rebuilder_class = PostgresSearchRebuilder
    atomic_rebuilder_class = PostgresSearchAtomicRebuilder

    def __init__(self, params):
        super().__init__(params)
        self.index_name = params.get('INDEX', 'default')
        self.config = params.get('SEARCH_CONFIG')
        if params.get('ATOMIC_REBUILD', False):
            self.rebuilder_class = self.atomic_rebuilder_class

    def get_index_for_model(self, model, db_alias=None):
        return Index(self, db_alias)

    def get_index_for_object(self, obj):
        return self.get_index_for_model(obj._meta.model, obj._state.db)

    def reset_index(self):
        for connection in get_postgresql_connections():
            IndexEntry._default_manager.using(connection.alias).filter(
                index_name=self.index_name
            ).delete()

    def add_type(self, model):
        pass  # Not needed.

    def refresh_index(self):
        pass  # Not needed.

    def add(self, obj):
        self.get_index_for_object(obj).add_item(obj)

    def add_bulk(self, model, obj_list):
        if obj_list:
            self.get_index_for_object(obj_list[0]).add_items(model, obj_list)

    def delete(self, obj):
        self.get_index_for_object(obj).delete_item(obj)


SearchBackend = PostgresSearchBackend
