import asyncio
import typing
from dataclasses import dataclass, field
from typing import List, Optional

import cloudpickle
import jsonpickle
from flyteidl.core.execution_pb2 import TaskExecution, TaskLog
from flytekitplugins.airflow.task import AirflowObj, _get_airflow_instance

from airflow.exceptions import AirflowException, TaskDeferred
from airflow.models import BaseOperator
from airflow.sensors.base import BaseSensorOperator
from airflow.triggers.base import BaseTrigger, TriggerEvent
from airflow.utils.context import Context
from flytekit import logger
from flytekit.exceptions.user import FlyteUserException
from flytekit.extend.backend.base_connector import AsyncConnectorBase, ConnectorRegistry, Resource, ResourceMeta
from flytekit.models.literals import LiteralMap
from flytekit.models.task import TaskTemplate


@dataclass
class AirflowMetadata(ResourceMeta):
    """
    This class is used to store the Airflow task configuration. It is serialized and returned to FlytePropeller.
    """

    airflow_operator: AirflowObj
    airflow_trigger: AirflowObj = field(default=None)
    airflow_trigger_callback: str = field(default=None)
    job_id: typing.Optional[str] = field(default=None)

    def encode(self) -> bytes:
        return cloudpickle.dumps(self)

    @classmethod
    def decode(cls, data: bytes) -> "AirflowMetadata":
        return cloudpickle.loads(data)


class AirflowConnector(AsyncConnectorBase):
    """
    It is used to run Airflow tasks.
    It is registered as an connector in the Connector Registry.
    There are three kinds of Airflow tasks: AirflowOperator, AirflowSensor, and AirflowHook.

    Sensor is always invoked in get method. Calling get method to check if the certain condition is met.
    For example, FileSensor is used to check if the file exists. If file doesn't exist, connector returns
    RUNNING status, otherwise, it returns SUCCEEDED status.

    Hook is a high-level interface to an external platform that lets you quickly and easily talk to
     them without having to write low-level code that hits their API or uses special libraries. For example,
     SlackHook is used to send messages to Slack. Therefore, Hooks are also invoked in get method.
    Note: There is no running state for Hook. It is either successful or failed.

    Operator is invoked in create method. Flytekit will always set deferrable to True for Operator. Therefore,
    `operator.execute` will always raise TaskDeferred exception after job is submitted. In the get method,
    we create a trigger to check if the job is finished.
    Note: some of the operators are not deferrable. For example, BeamRunJavaPipelineOperator, BeamRunPythonPipelineOperator.
     In this case, those operators will be converted to AirflowContainerTask and executed in the pod.
    """

    name = "Airflow Connector"

    def __init__(self):
        super().__init__(task_type_name="airflow", metadata_type=AirflowMetadata)

    async def create(
        self, task_template: TaskTemplate, inputs: Optional[LiteralMap] = None, **kwargs
    ) -> AirflowMetadata:
        airflow_obj = jsonpickle.decode(task_template.custom["task_config_pkl"])
        airflow_instance = _get_airflow_instance(airflow_obj)
        resource_meta = AirflowMetadata(airflow_operator=airflow_obj)

        if isinstance(airflow_instance, BaseOperator) and not isinstance(airflow_instance, BaseSensorOperator):
            try:
                resource_meta = AirflowMetadata(airflow_operator=airflow_obj)
                airflow_instance.execute(context=Context())
            except TaskDeferred as td:
                parameters = td.trigger.__dict__.copy()
                # Remove parameters that are in the base class
                parameters.pop("task_instance", None)
                parameters.pop("trigger_id", None)

                resource_meta.airflow_trigger = AirflowObj(
                    module=td.trigger.__module__, name=td.trigger.__class__.__name__, parameters=parameters
                )
                resource_meta.airflow_trigger_callback = td.method_name

        return resource_meta

    async def get(self, resource_meta: AirflowMetadata, **kwargs) -> Resource:
        airflow_operator_instance = _get_airflow_instance(resource_meta.airflow_operator)
        airflow_trigger_instance = (
            _get_airflow_instance(resource_meta.airflow_trigger) if resource_meta.airflow_trigger else None
        )
        airflow_ctx = Context()
        message = None
        cur_phase = TaskExecution.RUNNING

        if isinstance(airflow_operator_instance, BaseSensorOperator):
            ok = airflow_operator_instance.poke(context=airflow_ctx)
            cur_phase = TaskExecution.SUCCEEDED if ok else TaskExecution.RUNNING
        elif isinstance(airflow_operator_instance, BaseOperator):
            if airflow_trigger_instance:
                try:
                    # Airflow trigger returns immediately when
                    # 1. Failed to get task status
                    # 2. Task succeeded or failed
                    # succeeded or failed: returns a TriggerEvent with payload
                    # running: runs forever, so set a default timeout (2 seconds) here.
                    # failed to get the status: raises AirflowException
                    event = await asyncio.wait_for(airflow_trigger_instance.run().__anext__(), 2)
                    try:
                        # Trigger callback will check the status of the task in the payload, and raise AirflowException if failed.
                        trigger_callback = getattr(airflow_operator_instance, resource_meta.airflow_trigger_callback)
                        trigger_callback(context=airflow_ctx, event=typing.cast(TriggerEvent, event).payload)
                        cur_phase = TaskExecution.SUCCEEDED
                    except AirflowException as e:
                        cur_phase = TaskExecution.FAILED
                        message = e.__str__()
                except asyncio.TimeoutError:
                    logger.debug("No event received from airflow trigger")
                except AirflowException as e:
                    cur_phase = TaskExecution.FAILED
                    message = e.__str__()
            else:
                # If there is no trigger, it means the operator is not deferrable. In this case, this operator will be
                # executed in the creation step. Therefore, we can directly return to SUCCEEDED here.
                # For instance, SlackWebhookOperator is not deferrable. It sends a message to Slack in the creation step.
                # If the message is sent successfully, connector will return SUCCEEDED here. Otherwise, it will raise an exception at creation step.
                cur_phase = TaskExecution.SUCCEEDED

        else:
            raise FlyteUserException("Only sensor and operator are supported.")

        return Resource(
            phase=cur_phase,
            message=message,
            log_links=get_log_links(airflow_operator_instance, airflow_trigger_instance),
        )

    async def delete(self, resource_meta: AirflowMetadata, **kwargs):
        return


def get_log_links(
    airflow_operator: BaseOperator, airflow_trigger: Optional[BaseTrigger] = None
) -> Optional[List[TaskLog]]:
    log_links: List[TaskLog] = []
    try:
        from airflow.providers.google.cloud.operators.dataproc import DataprocJobBaseOperator, DataprocSubmitTrigger

        if isinstance(airflow_operator, DataprocJobBaseOperator):
            log_link = TaskLog(
                uri=f"https://console.cloud.google.com/dataproc/jobs/{typing.cast(DataprocSubmitTrigger, airflow_trigger).job_id}/monitoring?region={airflow_operator.region}&project={airflow_operator.project_id}",
                name="Dataproc Console",
            )
            log_links.append(log_link)
            return log_links
    except ImportError:
        ...
    return log_links


ConnectorRegistry.register(AirflowConnector())
