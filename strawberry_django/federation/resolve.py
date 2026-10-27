"""Federation reference resolution utilities for Django models.

This module provides utilities to automatically generate `resolve_reference`
class methods for federation entity types backed by Django models.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, cast, get_origin

from django.db import models
from strawberry.annotation import StrawberryAnnotation
from strawberry.scalars import ID
from strawberry.schema.types.scalar import DEFAULT_SCALAR_REGISTRY
from strawberry.types import get_object_definition
from strawberry.types.field import UNRESOLVED
from strawberry.types.info import Info
from strawberry.types.scalar import ScalarDefinition, ScalarWrapper
from strawberry.utils.await_maybe import AwaitableOrValue

from strawberry_django.queryset import run_type_get_queryset
from strawberry_django.resolvers import django_resolver
from strawberry_django.utils.typing import unwrap_type

if TYPE_CHECKING:
    from collections.abc import Mapping

    from strawberry_django.utils.typing import WithStrawberryDjangoObjectDefinition

_M = TypeVar("_M", bound=models.Model)

_PRIMITIVE_DEFAULT_SCALARS = (str, int, float, bool, ID)


__all__ = [
    "generate_resolve_reference",
    "resolve_model_reference",
]


def _get_key_field_type(
    cls: type[WithStrawberryDjangoObjectDefinition],
    key: str,
    info: Info | None = None,
) -> Any:
    type_def = get_object_definition(cls)
    if type_def is not None:
        name_converter = info.schema.config.name_converter if info is not None else None
        for field in type_def.fields:
            if key in {field.python_name, field.name, field.graphql_name} or (
                name_converter is not None and name_converter.from_field(field) == key
            ):
                resolved = field.resolve_type(type_definition=type_def)
                if resolved is not UNRESOLVED:
                    return unwrap_type(resolved)

    annotations = getattr(cls, "__annotations__", {})
    if key in annotations:
        return unwrap_type(StrawberryAnnotation(annotations[key]).resolve())

    return None


def _get_scalar_definition(
    field_type: Any,
    scalar_registry: Mapping[object, ScalarWrapper | ScalarDefinition],
) -> ScalarDefinition | None:
    if isinstance(field_type, ScalarDefinition):
        return field_type

    if isinstance(field_type, ScalarWrapper) or hasattr(
        field_type, "_scalar_definition"
    ):
        return field_type._scalar_definition

    target = field_type
    if target not in scalar_registry:
        origin = get_origin(target)
        if origin is not None and origin in scalar_registry:
            target = origin

    if target in scalar_registry:
        registered = scalar_registry[target]
        if isinstance(registered, ScalarWrapper):
            return registered._scalar_definition
        return registered

    return None


def _parse_key_value(
    cls: type[WithStrawberryDjangoObjectDefinition],
    key: str,
    value: Any,
    info: Info | None = None,
) -> Any:
    if value is None:
        return None

    field_type = _get_key_field_type(cls, key, info=info)
    if field_type is None:
        return value

    scalar_registry: Mapping[object, ScalarWrapper | ScalarDefinition] = (
        info.schema.schema_converter.scalar_registry
        if info is not None
        else DEFAULT_SCALAR_REGISTRY
    )

    scalar_def = _get_scalar_definition(field_type, scalar_registry)
    if scalar_def is None or scalar_def.parse_value is None:
        return value

    is_default_scalar = scalar_def is DEFAULT_SCALAR_REGISTRY.get(field_type)
    if is_default_scalar:
        if field_type in _PRIMITIVE_DEFAULT_SCALARS:
            return value
        if isinstance(field_type, type) and isinstance(value, field_type):
            return value

    return scalar_def.parse_value(value)


def resolve_model_reference(
    source: type[WithStrawberryDjangoObjectDefinition],
    *,
    info: Info | None = None,
    **key_fields: Any,
) -> AwaitableOrValue[_M]:
    """Resolve a Django model instance by federation key fields.

    Similar to `resolve_model_node` but works with arbitrary key fields.
    """
    from strawberry_django import optimizer  # avoid circular import
    from strawberry_django.utils.typing import get_django_definition

    django_type = get_django_definition(source, strict=True)
    model = cast("type[_M]", django_type.model)

    qs = model._default_manager.all()
    qs = run_type_get_queryset(qs, source, info)
    qs = qs.filter(**key_fields)

    if info is not None:
        ext = optimizer.optimizer.get()
        if ext is not None:
            # If optimizer extension is enabled, optimize this queryset
            qs = ext.optimize(qs, info=info)

    def _get_result() -> _M:
        return qs.get()

    return django_resolver(_get_result)()


def generate_resolve_reference(key_fields: list[str]) -> classmethod:
    """Generate a resolve_reference classmethod for a federation entity type.

    Only *key_fields* are forwarded to the ORM query — federation may pass
    extra fields (e.g. from @requires) that are not valid ORM lookups.
    """

    def resolve_reference(
        cls: type[WithStrawberryDjangoObjectDefinition],
        info: Info | None = None,
        **kwargs: Any,
    ) -> AwaitableOrValue[Any]:
        # Extract only the key fields from kwargs
        # (federation may pass additional fields)
        filtered_kwargs = {
            k: _parse_key_value(cls, k, v, info=info)
            for k, v in kwargs.items()
            if k in key_fields
        }
        if not filtered_kwargs:
            msg = (
                f"No matching key fields found in kwargs. "
                f"Expected one of {key_fields}, got {list(kwargs.keys())}."
            )
            raise ValueError(msg)
        return resolve_model_reference(cls, info=info, **filtered_kwargs)

    return classmethod(resolve_reference)
