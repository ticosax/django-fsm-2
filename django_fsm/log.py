from __future__ import annotations

import typing
from dataclasses import dataclass

import django.db.utils
from django.conf import settings
from django.db import models
from django.db import transaction
from django.db.migrations.operations.base import Operation
from django.db.migrations.operations.base import OperationCategory
from django.db.migrations.state import ModelState

from . import FSMLogDescriptor
from .models import StateLog
from .models import TransitionLogBase
from .signals import post_transition

if typing.TYPE_CHECKING:  # pragma: no cover
    from . import _Field
    from . import _StateValue


__all__ = [
    "StateLog",
    "TransitionLogBase",
    "track",
]


_ModelT = typing.TypeVar("_ModelT", bound=models.Model)


@dataclass(frozen=True)
class TrackConfig:
    log_model: type[TransitionLogBase]
    relation_field: str


_registry: dict[type[models.Model], TrackConfig] = {}

NOTSET = object()


def track(
    *,
    log_model: type[TransitionLogBase] | None = None,
    relation_field: str | None = None,
) -> typing.Callable[[type[_ModelT]], type[_ModelT]]:
    def decorator(model_cls: type[_ModelT]) -> type[_ModelT]:
        if model_cls._meta.abstract:  # pragma: no cover
            raise TypeError("django_fsm.track cannot be used with abstract models")

        _registry[model_cls] = TrackConfig(
            log_model=log_model or StateLog,
            relation_field=relation_field or "content_object",
        )

        post_transition.connect(
            _log_transition,
            sender=model_cls,
            dispatch_uid=f"django_fsm.track.{model_cls._meta.label_lower}",
            weak=False,
        )
        return model_cls

    return decorator


def _log_transition(
    sender: type[models.Model],
    instance: models.Model,
    name: str,
    source: _StateValue,
    target: _StateValue,
    field: _Field,
    **kwargs: typing.Any,
) -> None:
    config = _registry.get(sender)
    if not config or instance.pk is None:  # pragma: no cover
        return

    log_model = config.log_model or StateLog

    log_model._default_manager.using(instance._state.db).create(
        **{
            "transition": name,
            "state_field": field.name,
            "source_state": _coerce_state(source),
            "state": _coerce_state(target),
            "by": _extract_log_value(instance, "by"),
            "description": _extract_log_value(instance, "description"),
            config.relation_field: instance,
        }
    )


def _coerce_state(value: _StateValue | None) -> _StateValue | None:
    if isinstance(value, models.Model):
        return str(value.pk)
    return value


def _extract_log_value(instance: models.Model, attribute: str) -> typing.Any:
    return getattr(instance, f"{FSMLogDescriptor.ATTR_PREFIX}{attribute}", None)


class MigrateStateLogOperation(Operation):
    """
    Custom operation, faciliting ownership transfer of data contained in table,
    from one Model entity to another.
    Typical Example to transfer ownership from django_fsm_log.models.StateLog
    to django_fsm.log.StateLog model. But it can be reused to migrate
    django_fsm.log.StateLog data into your project ownership. To comply
    with the recommended "Bring Your Own model" design.
    """

    reduces_to_sql = False

    reversible = True
    category = OperationCategory.ALTERATION

    def __init__(self, from_model_table_name: str | None, to_model: ModelState):
        self.from_model_table_name = from_model_table_name
        self.to_model = to_model

    def state_forwards(self, app_label, state):
        if not self.from_model_table_name:
            return
        state.add_model(self.to_model)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if not self.from_model_table_name:
            return
        with transaction.atomic():
            try:
                schema_editor.alter_db_table(
                    None,
                    self.from_model_table_name,
                    f"{self.to_model.app_label}_{self.to_model.name_lower}",
                )
            except django.db.utils.ProgrammingError as err:
                if str(err) == f'relation "{self.from_model_table_name}" does not exist':
                    # from_model was not previously provisioned (test environment?).
                    # There is, no data migration to perform.
                    # instead create the model as if seen for the first time.
                    transaction.set_rollback(True)
                else:
                    raise
            else:
                return
        if settings.DJANGO_FSM_USE_LEGACY_STATE_LOG_MODEL:
            schema_editor.create_model(self.to_model.render(to_state.apps))

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if not self.from_model_table_name:
            return
        if settings.DJANGO_FSM_USE_LEGACY_STATE_LOG_MODEL:
            schema_editor.alter_db_table(
                None, self.to_model._meta.db_table, self.from_model_table_name
            )

    def describe(self):
        return "Transfer data ownership from one Model to another sharing same table structure"

    @property
    def migration_name_fragment(self) -> str:
        return f"migrate_model_{self.from_model_table_name}_{self.to_model._meta.model_name}"
