import functools
import warnings
import datetime

from django.core.exceptions import FieldDoesNotExist
from django.db.models import ForeignKey, Prefetch
from django.db.models.constants import LOOKUP_SEP
from django.db.models.fields.reverse_related import ManyToOneRel
from graphene import InputObjectType, GlobalID
from graphene.types.generic import GenericScalar
from graphene.types.resolver import default_resolver
from graphene_django import DjangoObjectType
from graphene_django.fields import DjangoListField

from graphene_django_plus.fields import DjangoListField, PlusListField, DjangoPlusListField, PlusFilterConnectionField

from graphql import ResolveInfo
from graphql.execution.base import (
    get_field_def,
)
from graphql.language.ast import (
    FragmentSpread,
    InlineFragment,
    Variable,
)
from graphql.type.definition import (
    GraphQLInterfaceType,
    GraphQLUnionType,
)

from .utils import is_iterable


def query(queryset, info, **options):
    """
    Automatically optimize queries.

    Arguments:
        - queryset (Django QuerySet object) - The queryset to be optimized
        - info (GraphQL ResolveInfo object) - This is passed by the graphene-django resolve methods
        - **options - optimization options/settings
            - disable_abort_only (boolean) - in case the objecttype contains any extra fields,
                                             then this will keep the "only" optimization enabled.
            - parent_id_field (string) - when a prefetched queryset is being optimized within the
                                         optimization hints of a parent queryset, the database id
                                         of the parent will have to be included in the "only" list
                                         to match the prefetched object to the parent.
    """

    return QueryOptimizer(info, **options).optimize(queryset)


class QueryOptimizer(object):
    """
    Automatically optimize queries.
    """

    def __init__(self, info, **options):
        self.root_info = info
        self.disable_abort_only = options.pop('disable_abort_only', False)
        self.parent_id_field = options.pop('parent_id_field', None)

        # Used if overriding resolve_id.
        self.id_field = options.get('id_field', 'id')

    def optimize(self, queryset, append_only=None):
        info = self.root_info
        field_def = get_field_def(info.schema, info.parent_type, info.field_name)
        store = self._optimize_gql_selections(
            self._get_type(field_def),
            info.field_asts[0],
            # info.parent_type,
        )
        if self.parent_id_field:
            store.append_only(self.parent_id_field)

        if hasattr(queryset, '_gql_parent_id_field'):
            store.append_only(getattr(queryset, '_gql_parent_id_field'))

        # Allow forcing attributes in only.
        if append_only:
            store.append_only_list += append_only

        return store.optimize_queryset(queryset)

    def _get_type(self, field_def):
        a_type = field_def.type
        while hasattr(a_type, 'of_type'):
            a_type = a_type.of_type
        return a_type

    def _get_possible_types(self, graphql_type):
        if isinstance(graphql_type, (GraphQLInterfaceType, GraphQLUnionType)):
            return self.root_info.schema.get_possible_types(graphql_type)
        else:
            return (graphql_type, )

    def _get_base_model(self, graphql_types):
        models = tuple(t.graphene_type._meta.model for t in graphql_types)
        for model in models:
            if all(issubclass(m, model) for m in models):
                return model
        return None

    def handle_inline_fragment(self, selection, schema, possible_types, store):
        fragment_type_name = selection.type_condition.name.value
        fragment_type = schema.get_type(fragment_type_name)
        fragment_possible_types = self._get_possible_types(fragment_type)
        for fragment_possible_type in fragment_possible_types:
            fragment_model = fragment_possible_type.graphene_type._meta.model
            parent_model = self._get_base_model(possible_types)
            if not parent_model:
                continue
            path_from_parent = _get_path_from_parent(
                fragment_model._meta, parent_model)
            select_related_name = LOOKUP_SEP.join(
                p.join_field.name for p in path_from_parent)
            if not select_related_name:
                continue
            fragment_store = self._optimize_gql_selections(
                fragment_possible_type,
                selection,
                # parent_type,
            )
            store.select_related(select_related_name, fragment_store)
        return store

    def handle_fragment_spread(self, store, name, field_type):
        fragment = self.root_info.fragments[name]
        fragment_store = self._optimize_gql_selections(
            field_type,
            fragment,
            # parent_type,
        )
        store.append(fragment_store)

    def _optimize_gql_selections(self, field_type, field_ast):
        store = QueryOptimizerStore(
            disable_abort_only=self.disable_abort_only,
        )

        selection_set = field_ast.selection_set
        if not selection_set:
            return store
        optimized_fields_by_model = {}
        schema = self.root_info.schema
        graphql_type = schema.get_graphql_type(field_type.graphene_type)
        possible_types = self._get_possible_types(graphql_type)
        for selection in selection_set.selections:
            if isinstance(selection, InlineFragment):
                self.handle_inline_fragment(
                    selection, schema, possible_types, store)
            else:
                name = selection.name.value
                if isinstance(selection, FragmentSpread):
                    self.handle_fragment_spread(store, name, field_type)
                else:
                    for possible_type in possible_types:
                        selection_field_def = possible_type.fields.get(name)
                        if not selection_field_def:
                            continue

                        graphene_type = possible_type.graphene_type
                        # Check if graphene type is a relay connection or a relay edge
                        if hasattr(graphene_type._meta, 'node') or (
                            hasattr(graphene_type, 'cursor')
                            and hasattr(graphene_type, 'node')
                        ):
                            relay_store = self._optimize_gql_selections(
                                self._get_type(selection_field_def),
                                selection,
                            )
                            store.append(relay_store)
                            try:
                                from django.db.models import DEFERRED  # noqa: F401
                            except ImportError:
                                store.abort_only_optimization(name)
                        else:
                            model = getattr(graphene_type._meta, 'model', None)
                            if model and name not in optimized_fields_by_model:
                                field_model = optimized_fields_by_model[name] = model
                                if field_model == model:
                                    self._optimize_field(
                                        store,
                                        model,
                                        selection,
                                        selection_field_def,
                                        possible_type,
                                        name
                                    )
        return store

    def _optimize_field(self, store, model, selection, field_def, parent_type, name):
        optimized_by_directive = self._optimize_field_by_directives(
            store, model, selection, field_def)

        if optimized_by_directive:
            return

        optimized_by_name = self._optimize_field_by_name(
            store, model, selection, field_def, parent_type)
        optimized_by_hints = self._optimize_field_by_hints(
            store, selection, field_def, parent_type)
        optimized = optimized_by_name or optimized_by_hints
        if not optimized:
            store.abort_only_optimization(name)

    def _optimize_field_by_directives(self, store, model, selection, field_def):
        variable_values = self.root_info.variable_values

        for directive in selection.directives:
            args = {}
            for arg in directive.arguments:
                if isinstance(arg.value, Variable):
                    var_name = arg.value.name.value
                    value = variable_values.get(var_name)
                else:
                    value = arg.value.value
                args[arg.name.value] = value

            if directive.name.value == 'include':
                return not args.get('if', True)
            elif directive.name.value == 'skip':
                return args.get('if', False)

        return False

    def _optimize_field_by_name(self, store, model, selection, field_def, parent_type):
        name, ignore = self._get_name_from_resolver(field_def.resolver, parent_type)
        if not name:
            return False
        if ignore:
            return True
        model_field = self._get_model_field_from_name(model, name)
        if not model_field:
            return False
        if self._is_foreign_key_id(model_field, name):
            store.only(name)
            return True
        if model_field.many_to_one or model_field.one_to_one:
            field_def_type = self._get_type(field_def)
            field_store = self._optimize_gql_selections(
                field_def_type,
                selection,
                # parent_type,
            )
            id_field = None
            if hasattr(field_def_type, 'graphene_type') and hasattr(field_def_type.graphene_type._meta, 'id_field'):
                id_field = field_def_type.graphene_type._meta.id_field
            store.select_related(name, field_store, model_field=model_field, id_field=id_field)
            return True
        if model_field.one_to_many or model_field.many_to_many:
            field_store = self._optimize_gql_selections(
                self._get_type(field_def),
                selection,
                # parent_type,
            )

            if isinstance(model_field, ManyToOneRel):
                field_store.only(model_field.field.name)

            related_queryset = model_field.related_model.objects.all()
            store.prefetch_related(name, field_store, related_queryset)
            return True
        if not model_field.is_relation:
            store.only(name)
            return True
        return False

    def _get_optimization_hints(self, resolver):
        resolver_fn = resolver
        if isinstance(resolver, functools.partial):
            if resolver.func == DjangoListField.list_resolver or resolver.func == DjangoPlusListField.list_resolver:
                resolver_fn = resolver.args[1]
            elif resolver.func == GlobalID.id_resolver and len(resolver.args) > 1:
                resolver_fn = resolver.args[0]
            elif resolver.func == PlusListField.list_resolver:
                resolver_fn = resolver.args[0]
            elif resolver.func == PlusFilterConnectionField.connection_resolver:
                resolver_fn = resolver.args[0]

        return getattr(resolver_fn, 'optimization_hints', None)

    def _get_value(self, info, value):
        if isinstance(value, Variable):
            var_name = value.name.value
            value = info.variable_values.get(var_name)
            return value
        if isinstance(value, InputObjectType):
            return value.__dict__
        if isinstance(value, float) or isinstance(value, str) or isinstance(value, datetime.date) or isinstance(value, datetime.datetime):
            return value
        else:
            return GenericScalar.parse_literal(value)
        # return value

    def _optimize_field_by_hints(self, store, selection, field_def, parent_type):
        optimization_hints = self._get_optimization_hints(field_def.resolver)
        if not optimization_hints:
            return False
        info = self._create_resolve_info(
            selection.name.value,
            (selection,),
            self._get_type(field_def),
            parent_type,
        )

        args = []
        for arg in selection.arguments:
            args.append(self._get_value(info, arg.value))
        args = tuple(args)

        self._add_optimization_hints(
            optimization_hints.select_related(info, *args),
            store.select_list,
        )
        self._add_optimization_hints(
            optimization_hints.prefetch_related(info, *args),
            store.prefetch_list,
            store.prefetch_not_applied,
            optimization_hints.apply_prefetch_related,
        )
        self._add_optimization_hints(
            optimization_hints.annotate(info, *args),
            store.annotate_dict,
        )
        if store.only_list is not None:
            self._add_optimization_hints(
                optimization_hints.only(info, *args),
                store.only_list,
            )
        return True

    def _add_optimization_hints(self, source, target, not_applied=None, should_apply=True):
        if source:
            if not is_iterable(source):
                source = (source,)

            if should_apply or not_applied is None:
                if isinstance(target, dict):
                    target.update(source)
                else:
                    target += source
            else:
                if isinstance(not_applied, dict):
                    not_applied.update(source)
                else:
                    not_applied += source

    def _get_name_from_resolver(self, resolver, parent_type):
        optimization_hints = self._get_optimization_hints(resolver)
        if optimization_hints:
            name_fn = optimization_hints.model_field
            if name_fn:
                return name_fn(), optimization_hints.ignore
        if self._is_resolver_for_id_field(resolver):
            if hasattr(parent_type, 'graphene_type') and hasattr(parent_type.graphene_type._meta, 'id_field'):
                return parent_type.graphene_type._meta.id_field, False

            return self.id_field, False
        elif isinstance(resolver, functools.partial):
            resolver_fn = resolver
            if resolver_fn.func != default_resolver:
                # Some resolvers have the partial function as the second
                # argument.
                for arg in resolver_fn.args:
                    if isinstance(arg, (str, functools.partial)):
                        break
                else:
                    # No suitable instances found, default to first arg
                    arg = resolver_fn.args[0]
                resolver_fn = arg
            if isinstance(resolver_fn, functools.partial) and resolver_fn.func == default_resolver:
                return resolver_fn.args[0], False
            return resolver_fn, False
        return None, False

    def _is_resolver_for_id_field(self, resolver):
        resolve_id = DjangoObjectType.resolve_id
        # For python 2 unbound method:
        if hasattr(resolve_id, 'im_func'):
            resolve_id = resolve_id.im_func

        if isinstance(resolver, functools.partial):
            resolver_fn = resolver

            if resolver_fn.func == GlobalID.id_resolver:
                # This would return False if resolve_id is overriden
                # by the Type. Instead check for the name of the
                # function for safety.
                # if resolver_fn.args:
                #     return resolver_fn.args[0] == resolve_id

                if resolver_fn.args:
                    return resolver_fn.args[0].__name__ == 'resolve_id'

        return resolver == resolve_id

    def _get_model_field_from_name(self, model, name):
        try:
            return model._meta.get_field(name)
        except FieldDoesNotExist:
            descriptor = model.__dict__.get(name)
            if not descriptor:
                return None
            return getattr(descriptor, 'rel', None) \
                or getattr(descriptor, 'related', None)  # Django < 1.9

    def _is_foreign_key_id(self, model_field, name):
        return (
            isinstance(model_field, ForeignKey)
            and model_field.name != name
            and model_field.get_attname() == name
        )

    def _create_resolve_info(self, field_name, field_asts, return_type, parent_type):
        return ResolveInfo(
            field_name,
            field_asts,
            return_type,
            parent_type,
            schema=self.root_info.schema,
            fragments=self.root_info.fragments,
            root_value=self.root_info.root_value,
            operation=self.root_info.operation,
            variable_values=self.root_info.variable_values,
            context=self.root_info.context,
        )


class QueryOptimizerStore():
    def __init__(self, disable_abort_only=False):
        self.select_list = []
        self.prefetch_list = []
        self.annotate_dict = {}
        self.only_list = []
        self.disable_abort_only = disable_abort_only

        # Store prefetches that have been saved but will not be applied.
        self.prefetch_not_applied = []

        # A list of values to force append to 'only' if only is used.
        self.append_only_list = []

    def select_related(self, name, store, model_field=None, id_field=None):
        if store.annotate_dict:
            self.append_only(model_field.attname)
            self.prefetch_related(name, store, model_field.related_model.objects.all(), id_field=id_field)
        else:
            if store.select_list:
                for select in store.select_list:
                    self.select_list.append(name + LOOKUP_SEP + select)
            else:
                self.select_list.append(name)
            for prefetch in store.prefetch_list:
                if isinstance(prefetch, Prefetch):
                    prefetch.add_prefix(name)
                else:
                    prefetch = name + LOOKUP_SEP + prefetch
                self.prefetch_list.append(prefetch)
            if self.only_list is not None:
                if store.only_list is None:
                    self.abort_only_optimization(name)
                else:
                    if id_field and id_field != "pk":
                        self.only_list.append(name + LOOKUP_SEP + id_field)
                    for only in store.only_list:
                        self.only_list.append(name + LOOKUP_SEP + only)
                    if store.append_only_list:
                        for append_only in store.append_only_list:
                            self.append_only_list.append(name + LOOKUP_SEP + append_only)

    def prefetch_related(self, name, store, queryset, attname=None, id_field=None):
        if store.select_list or store.only_list:
            if attname and store.only_list:
                store.only_list.append(attname)
            if id_field and store.only_list and id_field != "pk":
                store.only_list.append(id_field)

            queryset = store.optimize_queryset(queryset)
            self.prefetch_list.append(Prefetch(name, queryset=queryset))
        elif store.prefetch_list:
            for prefetch in store.prefetch_list:
                if isinstance(prefetch, Prefetch):
                    prefetch.add_prefix(name)
                else:
                    prefetch = name + LOOKUP_SEP + prefetch
                self.prefetch_list.append(prefetch)
        else:
            self.prefetch_list.append(name)

    def only(self, field):
        if self.only_list is not None:
            self.only_list.append(field)

    def append_only(self, field):
        self.append_only_list.append(field)

    def abort_only_optimization(self, field=None):
        if not self.disable_abort_only:
            self.only_list = None
            warnings.warn("Query optimization aborted, could not optimize field: {}.".format(field), UserWarning)

    def optimize_queryset(self, queryset):
        if self.select_list:
            queryset = queryset.select_related(*self.select_list)

        if self.prefetch_list:
            queryset = queryset.prefetch_related(*self.prefetch_list)

        if self.annotate_dict:
            queryset = queryset.annotate(**self.annotate_dict)

        if self.only_list and self.append_only_list:
            queryset = queryset.only(*self.only_list + self.append_only_list)
        elif self.only_list:
            queryset = queryset.only(*self.only_list)

        setattr(queryset, '_gql_optimized', self.only_list is not None)

        return queryset

    def append(self, store):
        self.select_list += store.select_list
        self.prefetch_list += store.prefetch_list
        self.annotate_dict.update(store.annotate_dict)
        self.prefetch_not_applied += store.prefetch_not_applied
        if self.only_list is not None:
            if store.only_list is None:
                self.only_list = None
            else:
                self.only_list += store.only_list
        if self.append_only_list is not None:
            if store.append_only_list is None:
                self.append_only_list = None
            else:
                self.append_only_list += store.append_only_list

# For legacy Django versions:
def _get_path_from_parent(self, parent):
    """
    Return a list of PathInfos containing the path from the parent
    model to the current model, or an empty list if parent is not a
    parent of the current model.
    """
    if hasattr(self, 'get_path_from_parent'):
        return self.get_path_from_parent(parent)
    if self.model is parent:
        return []
    model = self.concrete_model
    # Get a reversed base chain including both the current and parent
    # models.
    chain = model._meta.get_base_chain(parent) or []
    chain.reverse()
    chain.append(model)
    # Construct a list of the PathInfos between models in chain.
    path = []
    for i, ancestor in enumerate(chain[:-1]):
        child = chain[i + 1]
        link = child._meta.get_ancestor_link(ancestor)
        path.extend(link.get_reverse_path_info())
    return path
