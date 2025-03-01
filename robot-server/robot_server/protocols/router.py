"""Router for /protocols endpoints."""

import json
import logging
from textwrap import dedent
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union, Tuple

from opentrons.protocol_engine.types import RunTimeParamValuesType
from opentrons_shared_data.robot import user_facing_robot_type
from opentrons.util.performance_helpers import TrackingFunctions
from typing_extensions import Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
    Form,
)
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from opentrons.protocol_reader import (
    ProtocolReader,
    ProtocolFilesInvalidError,
    FileReaderWriter,
    FileHasher,
)
from opentrons_shared_data.robot.dev_types import RobotType

from robot_server.errors.error_responses import ErrorDetails, ErrorBody
from robot_server.hardware import get_robot_type
from robot_server.service.dependencies import get_unique_id, get_current_time
from robot_server.service.json_api import (
    Body,
    SimpleBody,
    SimpleMultiBody,
    SimpleEmptyBody,
    MultiBodyMeta,
    PydanticResponse,
    RequestModel,
)
from .analyses_manager import AnalysesManager

from .protocol_auto_deleter import ProtocolAutoDeleter
from .protocol_models import Protocol, ProtocolFile, Metadata, ProtocolKind
from .analysis_store import AnalysisStore, AnalysisNotFoundError, AnalysisIsPendingError
from .analysis_models import ProtocolAnalysis, AnalysisRequest, AnalysisSummary
from .protocol_store import (
    ProtocolStore,
    ProtocolResource,
    ProtocolNotFoundError,
    ProtocolUsedByRunError,
)
from .dependencies import (
    get_protocol_auto_deleter,
    get_protocol_reader,
    get_protocol_store,
    get_analysis_store,
    get_analyses_manager,
    get_protocol_directory,
    get_file_reader_writer,
    get_file_hasher,
)


log = logging.getLogger(__name__)


class ProtocolNotFound(ErrorDetails):
    """An error returned when a given protocol cannot be found."""

    id: Literal["ProtocolNotFound"] = "ProtocolNotFound"
    title: str = "Protocol Not Found"


class AnalysisNotFound(ErrorDetails):
    """An error returned when a given protocol analysis cannot be found."""

    id: Literal["AnalysisNotFound"] = "AnalysisNotFound"
    title: str = "Protocol Analysis Not Found"


class LastAnalysisPending(ErrorDetails):
    """An error returned when the most recent analysis of a protocol is still pending."""

    id: Literal["LastAnalysisPending"] = "LastAnalysisPending"
    title: str = "Last Analysis Still Pending."


class ProtocolFilesInvalid(ErrorDetails):
    """An error returned when an uploaded protocol files are invalid."""

    id: Literal["ProtocolFilesInvalid"] = "ProtocolFilesInvalid"
    title: str = "Protocol File(s) Invalid"


class ProtocolRobotTypeMismatch(ErrorDetails):
    """An error returned when an uploaded protocol is for a different type of robot.

    For example, if the protocol is for an OT-3, but this server is running on an OT-2.
    """

    id: Literal["ProtocolRobotTypeMismatch"] = "ProtocolRobotTypeMismatch"
    title: str = "Protocol For Different Robot Type"


class ProtocolUsedByRun(ErrorDetails):
    """An error returned when a protocol is used by a run and cannot be deleted."""

    id: Literal["ProtocolUsedByRun"] = "ProtocolUsedByRun"
    title: str = "Protocol Used by Run"


class RunLink(BaseModel):
    """Link to a run resource."""

    id: str = Field(..., description="The run's id")
    href: str = Field(..., description="The run's URL")


class ProtocolLinks(BaseModel):
    """Links returned along with a protocol resource."""

    referencingRuns: List[RunLink] = Field(
        ...,
        description=(
            "Links to runs that reference the protocol,"
            " in order from the oldest run to the newest run."
        ),
    )


protocols_router = APIRouter()


@PydanticResponse.wrap_route(
    protocols_router.post,
    path="/protocols",
    summary="Upload a protocol",
    description=dedent(
        """
        Upload a protocol to your device. You may include the following files:

        - A single Python protocol file and 0 or more custom labware JSON files
        - A single JSON protocol file (any additional labware files will be ignored)

        When too many protocols already exist, old ones will be automatically deleted
        to make room for the new one.
        A protocol will never be automatically deleted if there's a run
        referring to it, though. (See the `/runs/` endpoints.)

        If you upload the exact same set of files multiple times, the first protocol
        resource will be returned instead of creating duplicate ones.

        When a new protocol resource is created, an analysis is started for it.
        A new analysis is also started if the same protocol file is uploaded but with
        a different set of run-time parameter values than the most recent request.
        See the `/protocols/{id}/analyses/` endpoints for more details.

        You can provide the kind of protocol with the `protocol_kind` form data
        The protocol kind can be:

        - `quick-transfer` for Quick Transfer protocols
        - `standard`       for non Quick transfer protocols

        if the `protocol_kind` is None it will be defaulted to `standard`.

        Quick transfer protocols:
        - Do not store any run history
        - Do not get auto deleted, instead they have a fixed max count.
        """
    ),
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {"model": SimpleBody[Protocol]},
        status.HTTP_201_CREATED: {"model": SimpleBody[Protocol]},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": ErrorBody[Union[ProtocolFilesInvalid, ProtocolRobotTypeMismatch]]
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody[LastAnalysisPending]},
    },
)
async def create_protocol(  # noqa: C901
    files: List[UploadFile] = File(...),
    # use Form because request is multipart/form-data
    # https://fastapi.tiangolo.com/tutorial/request-forms-and-files/
    key: Optional[str] = Form(
        default=None,
        description=(
            "An arbitrary client-defined string to attach to the new protocol resource."
            " This should be no longer than ~100 characters or so."
            " It's intended to store something like a UUID, to help clients that store"
            " protocols locally keep track of which local files correspond to which"
            " protocol resources on the robot."
        ),
    ),
    run_time_parameter_values: Optional[str] = Form(
        default=None,
        description="Key-value pairs of run-time parameters defined in a protocol."
        " Note that this is expected to be a string holding a JSON object."
        " Also, if this data is included in the request, the server will"
        " always trigger an analysis (for now).",
        alias="runTimeParameterValues",
    ),
    protocol_kind: Optional[str] = Form(
        default=None,
        description=(
            "Whether this is a `standard` protocol or a `quick-transfer` protocol."
            "if ommited, the protocol will be `standard` by default."
        ),
        alias="protocolKind",
    ),
    protocol_directory: Path = Depends(get_protocol_directory),
    protocol_store: ProtocolStore = Depends(get_protocol_store),
    analysis_store: AnalysisStore = Depends(get_analysis_store),
    file_reader_writer: FileReaderWriter = Depends(get_file_reader_writer),
    protocol_reader: ProtocolReader = Depends(get_protocol_reader),
    file_hasher: FileHasher = Depends(get_file_hasher),
    analyses_manager: AnalysesManager = Depends(get_analyses_manager),
    protocol_auto_deleter: ProtocolAutoDeleter = Depends(get_protocol_auto_deleter),
    robot_type: RobotType = Depends(get_robot_type),
    protocol_id: str = Depends(get_unique_id, use_cache=False),
    analysis_id: str = Depends(get_unique_id, use_cache=False),
    created_at: datetime = Depends(get_current_time),
) -> PydanticResponse[SimpleBody[Protocol]]:
    """Create a new protocol by uploading its files.

    Arguments:
        files: List of uploaded files, from form-data.
        key: Optional key for cli-side tracking
        run_time_parameter_values: Key value pairs of run-time parameters defined in a protocol.
        protocol_kind: Optional key representing the kind of protocol.
        protocol_directory: Location to store uploaded files.
        protocol_store: In-memory database of protocol resources.
        analysis_store: In-memory database of protocol analyses.
        file_hasher: File hashing interface.
        file_reader_writer: Input file reader/writer.
        protocol_reader: Protocol file reading interface.
        analyses_manager: Protocol analysis managing interface.
        protocol_auto_deleter: An interface to delete old resources to make room for
            the new protocol.
        robot_type: The type of this robot. Protocols meant for other robot types
            are rejected.
        protocol_id: Unique identifier to attach to the protocol resource.
        analysis_id: Unique identifier to attach to the analysis resource.
        created_at: Timestamp to attach to the new resource.
    """
    kind = ProtocolKind.from_string(protocol_kind)
    if isinstance(protocol_kind, str) and kind is None:
        raise HTTPException(
            status_code=400, detail=f"Invalid protocol_kind: {protocol_kind}"
        )
    kind = kind or ProtocolKind.STANDARD

    for file in files:
        # TODO(mm, 2024-02-07): Investigate whether the filename can actually be None.
        assert file.filename is not None
    buffered_files = await file_reader_writer.read(files=files)  # type: ignore[arg-type]
    if isinstance(run_time_parameter_values, str):
        # We have to do this isinstance check because if `runTimeParameterValues` is
        # not specified in the request, then it gets assigned a Form(None) value
        # instead of just a None. \(O.o)/
        # TODO: check if we can make our own "RTP multipart-form field" Pydantic type
        #  so we can validate the data contents and return a better error response.
        parsed_rtp = json.loads(run_time_parameter_values)
    else:
        parsed_rtp = {}
    content_hash = await file_hasher.hash(buffered_files)
    cached_protocol_id = protocol_store.get_id_by_hash(content_hash)

    if cached_protocol_id is not None:

        @TrackingFunctions.track_getting_cached_protocol_analysis
        async def _get_cached_protocol_analysis() -> PydanticResponse[
            SimpleBody[Protocol]
        ]:
            resource = protocol_store.get(protocol_id=cached_protocol_id)
            try:
                analysis_summaries, _ = await _start_new_analysis_if_necessary(
                    protocol_id=cached_protocol_id,
                    analysis_id=analysis_id,
                    rtp_values=parsed_rtp,
                    force_reanalyze=False,
                    protocol_store=protocol_store,
                    analysis_store=analysis_store,
                    analyses_manager=analyses_manager,
                )
            except AnalysisIsPendingError as error:
                raise LastAnalysisPending(detail=str(error)).as_error(
                    status.HTTP_503_SERVICE_UNAVAILABLE
                ) from error

            data = Protocol.construct(
                id=cached_protocol_id,
                createdAt=resource.created_at,
                protocolKind=ProtocolKind.from_string(resource.protocol_kind),
                protocolType=resource.source.config.protocol_type,
                robotType=resource.source.robot_type,
                metadata=Metadata.parse_obj(resource.source.metadata),
                analysisSummaries=analysis_summaries,
                key=resource.protocol_key,
                files=[
                    ProtocolFile(name=f.path.name, role=f.role)
                    for f in resource.source.files
                ],
            )

            log.info(
                f'Protocol with id "{cached_protocol_id}" with same contents already exists.'
                f" Returning existing protocol data in response payload."
            )

            return await PydanticResponse.create(
                content=SimpleBody.construct(data=data),
                # not returning a 201 because we're not actually creating a new resource
                status_code=status.HTTP_200_OK,
            )

        return await _get_cached_protocol_analysis()

    try:
        source = await protocol_reader.save(
            files=buffered_files,
            directory=protocol_directory / protocol_id,
            content_hash=content_hash,
        )
    except ProtocolFilesInvalidError as e:
        raise ProtocolFilesInvalid(detail=str(e)).as_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY
        ) from e

    if source.robot_type != robot_type:
        raise ProtocolRobotTypeMismatch(
            detail=(
                f"This protocol is for {user_facing_robot_type(source.robot_type)} robots."
                f" It can't be analyzed or run on this robot,"
                f" which is {user_facing_robot_type(robot_type, include_article=True)}."
            )
        ).as_error(status.HTTP_422_UNPROCESSABLE_ENTITY)

    protocol_resource = ProtocolResource(
        protocol_id=protocol_id,
        created_at=created_at,
        source=source,
        protocol_key=key,
        protocol_kind=kind.value,
    )

    protocol_auto_deleter.make_room_for_new_protocol()
    protocol_store.insert(protocol_resource)

    new_analysis_summary = await analyses_manager.start_analysis(
        analysis_id=analysis_id,
        protocol_resource=protocol_resource,
        run_time_param_values=parsed_rtp,
    )

    data = Protocol(
        id=protocol_id,
        createdAt=created_at,
        protocolKind=kind,
        protocolType=source.config.protocol_type,
        robotType=source.robot_type,
        metadata=Metadata.parse_obj(source.metadata),
        analysisSummaries=[new_analysis_summary],
        key=key,
        files=[ProtocolFile(name=f.path.name, role=f.role) for f in source.files],
    )

    log.info(f'Created protocol "{protocol_id}" and started analysis "{analysis_id}".')

    return await PydanticResponse.create(
        content=SimpleBody.construct(data=data),
        status_code=status.HTTP_201_CREATED,
    )


async def _start_new_analysis_if_necessary(
    protocol_id: str,
    analysis_id: str,
    force_reanalyze: bool,
    rtp_values: RunTimeParamValuesType,
    protocol_store: ProtocolStore,
    analysis_store: AnalysisStore,
    analyses_manager: AnalysesManager,
) -> Tuple[List[AnalysisSummary], bool]:
    """Check RTP values and start a new analysis if necessary.

    Returns a tuple of the latest list of analysis summaries (including any newly
    started analysis) and whether a new analysis was started.
    """
    resource = protocol_store.get(protocol_id=protocol_id)
    analyses = analysis_store.get_summaries_by_protocol(protocol_id=protocol_id)
    started_new_analysis = False
    if (
        force_reanalyze
        or
        # Unexpected situations, like powering off the robot after a protocol upload
        # but before the analysis is complete, can leave the protocol resource
        # without an associated analysis.
        len(analyses) == 0
        or
        # The most recent analysis was done using different RTP values
        not await analysis_store.matching_rtp_values_in_analysis(
            analysis_summary=analyses[-1], new_rtp_values=rtp_values
        )
    ):
        started_new_analysis = True
        analyses.append(
            await analyses_manager.start_analysis(
                analysis_id=analysis_id,
                protocol_resource=resource,
                run_time_param_values=rtp_values,
            )
        )

    return analyses, started_new_analysis


@PydanticResponse.wrap_route(
    protocols_router.get,
    path="/protocols",
    summary="Get uploaded protocols",
    description="Return all stored protocols, in order from first-uploaded to last-uploaded.",
    responses={status.HTTP_200_OK: {"model": SimpleMultiBody[Protocol]}},
)
async def get_protocols(
    protocol_store: ProtocolStore = Depends(get_protocol_store),
    analysis_store: AnalysisStore = Depends(get_analysis_store),
) -> PydanticResponse[SimpleMultiBody[Protocol]]:
    """Get a list of all currently uploaded protocols.

    Args:
        protocol_store: In-memory database of protocol resources.
        analysis_store: In-memory database of protocol analyses.
    """
    protocol_resources = protocol_store.get_all()
    data = [
        Protocol.construct(
            id=r.protocol_id,
            createdAt=r.created_at,
            protocolKind=ProtocolKind.from_string(r.protocol_kind),
            protocolType=r.source.config.protocol_type,
            robotType=r.source.robot_type,
            metadata=Metadata.parse_obj(r.source.metadata),
            analysisSummaries=analysis_store.get_summaries_by_protocol(r.protocol_id),
            key=r.protocol_key,
            files=[ProtocolFile(name=f.path.name, role=f.role) for f in r.source.files],
        )
        for r in protocol_resources
    ]
    meta = MultiBodyMeta(cursor=0, totalLength=len(data))

    return await PydanticResponse.create(
        content=SimpleMultiBody.construct(data=data, meta=meta),
        status_code=status.HTTP_200_OK,
    )


@PydanticResponse.wrap_route(
    protocols_router.get,
    path="/protocols/ids",
    summary="[Internal] Get uploaded protocol IDs",
    description=(
        "Get the IDs of all protocols stored on the server."
        "\n\n"
        "**Warning:**"
        " This is an experimental endpoint and is only meant for internal use by Opentrons."
        " We might remove it or change its behavior without warning."
    ),
    responses={status.HTTP_200_OK: {"model": SimpleMultiBody[str]}},
)
async def get_protocol_ids(
    protocol_store: ProtocolStore = Depends(get_protocol_store),
) -> PydanticResponse[SimpleMultiBody[str]]:
    """Get a list of all protocol ids stored on the server.

    Args:
        protocol_store: In-memory database of protocol resources.
    """
    protocol_ids = protocol_store.get_all_ids()

    meta = MultiBodyMeta(cursor=0, totalLength=len(protocol_ids))

    return await PydanticResponse.create(
        content=SimpleMultiBody.construct(data=protocol_ids, meta=meta)
    )


@PydanticResponse.wrap_route(
    protocols_router.get,
    path="/protocols/{protocolId}",
    summary="Get an uploaded protocol",
    responses={
        status.HTTP_200_OK: {"model": Body[Protocol, ProtocolLinks]},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody[ProtocolNotFound]},
    },
)
async def get_protocol_by_id(
    protocolId: str,
    protocol_store: ProtocolStore = Depends(get_protocol_store),
    analysis_store: AnalysisStore = Depends(get_analysis_store),
) -> PydanticResponse[Body[Protocol, ProtocolLinks]]:
    """Get an uploaded protocol by ID.

    Args:
        protocolId: Protocol identifier to fetch, pulled from URL.
        protocol_store: In-memory database of protocol resources.
        analysis_store: In-memory database of protocol analyses.
    """
    try:
        resource = protocol_store.get(protocol_id=protocolId)
    except ProtocolNotFoundError as e:
        raise ProtocolNotFound(detail=str(e)).as_error(status.HTTP_404_NOT_FOUND)

    analyses = analysis_store.get_summaries_by_protocol(protocol_id=protocolId)
    referencing_run_ids = protocol_store.get_referencing_run_ids(protocolId)

    data = Protocol.construct(
        id=protocolId,
        createdAt=resource.created_at,
        protocolKind=ProtocolKind.from_string(resource.protocol_kind),
        protocolType=resource.source.config.protocol_type,
        robotType=resource.source.robot_type,
        metadata=Metadata.parse_obj(resource.source.metadata),
        analysisSummaries=analyses,
        key=resource.protocol_key,
        files=[
            ProtocolFile(name=f.path.name, role=f.role) for f in resource.source.files
        ],
    )

    links = ProtocolLinks.construct(
        referencingRuns=[
            RunLink.construct(id=run_id, href=f"/runs/{run_id}")
            for run_id in referencing_run_ids
        ]
    )

    return await PydanticResponse.create(
        content=Body.construct(
            data=data,
            links=links,
        ),
        status_code=status.HTTP_200_OK,
    )


@PydanticResponse.wrap_route(
    protocols_router.delete,
    path="/protocols/{protocolId}",
    summary="Delete an uploaded protocol",
    responses={
        status.HTTP_200_OK: {"model": SimpleEmptyBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody[ProtocolNotFound]},
        status.HTTP_409_CONFLICT: {"model": ErrorBody[ProtocolUsedByRun]},
    },
)
async def delete_protocol_by_id(
    protocolId: str,
    protocol_store: ProtocolStore = Depends(get_protocol_store),
) -> PydanticResponse[SimpleEmptyBody]:
    """Delete an uploaded protocol by ID.

    Arguments:
        protocolId: Protocol identifier to delete, pulled from URL.
        protocol_store: In-memory database of protocol resources.
    """
    try:
        protocol_store.remove(protocol_id=protocolId)

    except ProtocolNotFoundError as e:
        raise ProtocolNotFound(detail=str(e)).as_error(status.HTTP_404_NOT_FOUND) from e

    except ProtocolUsedByRunError as e:
        raise ProtocolUsedByRun(detail=str(e)).as_error(status.HTTP_409_CONFLICT) from e

    return await PydanticResponse.create(
        content=SimpleEmptyBody.construct(),
        status_code=status.HTTP_200_OK,
    )


@PydanticResponse.wrap_route(
    protocols_router.post,
    path="/protocols/{protocolId}/analyses",
    summary="Analyze the protocol",
    description=dedent(
        """
        Generate an analysis for the protocol, based on last analysis and current request data.
        """
    ),
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {"model": SimpleMultiBody[AnalysisSummary]},
        status.HTTP_201_CREATED: {"model": SimpleMultiBody[AnalysisSummary]},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody[ProtocolNotFound]},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody[LastAnalysisPending]},
    },
)
async def create_protocol_analysis(
    protocolId: str,
    request_body: Optional[RequestModel[AnalysisRequest]] = None,
    protocol_store: ProtocolStore = Depends(get_protocol_store),
    analysis_store: AnalysisStore = Depends(get_analysis_store),
    analyses_manager: AnalysesManager = Depends(get_analyses_manager),
    analysis_id: str = Depends(get_unique_id, use_cache=False),
) -> PydanticResponse[SimpleMultiBody[AnalysisSummary]]:
    """Start a new analysis for the given existing protocol.

    Starts a new analysis for the protocol along with the provided run-time parameter
    values (if any), and appends it to the existing analyses.

    If the last analysis in the existing analyses used the same RTP values, then a new
    analysis is not created.

    If `forceAnalyze` is True, this will always start a new analysis.

    Returns: List of analysis summaries available for the protocol, ordered as
             most recently started analysis last.
    """
    if not protocol_store.has(protocolId):
        raise ProtocolNotFound(detail=f"Protocol {protocolId} not found").as_error(
            status.HTTP_404_NOT_FOUND
        )
    try:
        (
            analysis_summaries,
            started_new_analysis,
        ) = await _start_new_analysis_if_necessary(
            protocol_id=protocolId,
            analysis_id=analysis_id,
            rtp_values=request_body.data.runTimeParameterValues if request_body else {},
            force_reanalyze=request_body.data.forceReAnalyze if request_body else False,
            protocol_store=protocol_store,
            analysis_store=analysis_store,
            analyses_manager=analyses_manager,
        )
    except AnalysisIsPendingError as error:
        raise LastAnalysisPending(detail=str(error)).as_error(
            status.HTTP_503_SERVICE_UNAVAILABLE
        ) from error
    return await PydanticResponse.create(
        content=SimpleMultiBody.construct(
            data=analysis_summaries,
            meta=MultiBodyMeta(cursor=0, totalLength=len(analysis_summaries)),
        ),
        status_code=(
            status.HTTP_201_CREATED if started_new_analysis else status.HTTP_200_OK
        ),
    )


@PydanticResponse.wrap_route(
    protocols_router.get,
    path="/protocols/{protocolId}/analyses",
    summary="Get a protocol's analyses",
    responses={
        status.HTTP_200_OK: {"model": SimpleMultiBody[ProtocolAnalysis]},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody[ProtocolNotFound]},
    },
)
async def get_protocol_analyses(
    protocolId: str,
    protocol_store: ProtocolStore = Depends(get_protocol_store),
    analysis_store: AnalysisStore = Depends(get_analysis_store),
) -> PydanticResponse[SimpleMultiBody[ProtocolAnalysis]]:
    """Get a protocol's full analyses list.

    Analyses are returned in order from least-recently started to most-recently started.

    Arguments:
        protocolId: Protocol identifier to delete, pulled from URL.
        protocol_store: Database of protocol resources.
        analysis_store: Database of analysis resources.
    """
    if not protocol_store.has(protocolId):
        raise ProtocolNotFound(detail=f"Protocol {protocolId} not found").as_error(
            status.HTTP_404_NOT_FOUND
        )

    analyses = await analysis_store.get_by_protocol(protocolId)

    return await PydanticResponse.create(
        content=SimpleMultiBody.construct(
            data=analyses,
            meta=MultiBodyMeta(cursor=0, totalLength=len(analyses)),
        )
    )


@PydanticResponse.wrap_route(
    protocols_router.get,
    path="/protocols/{protocolId}/analyses/{analysisId}",
    summary="Get one of a protocol's analyses",
    responses={
        status.HTTP_200_OK: {"model": SimpleBody[ProtocolAnalysis]},
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorBody[Union[ProtocolNotFound, AnalysisNotFound]]
        },
    },
)
async def get_protocol_analysis_by_id(
    protocolId: str,
    analysisId: str,
    protocol_store: ProtocolStore = Depends(get_protocol_store),
    analysis_store: AnalysisStore = Depends(get_analysis_store),
) -> PydanticResponse[SimpleBody[ProtocolAnalysis]]:
    """Get a protocol analysis by analysis ID.

    Arguments:
        protocolId: The ID of the protocol, pulled from the URL.
        analysisId: The ID of the analysis, pulled from the URL.
        protocol_store: Protocol resource storage.
        analysis_store: Analysis resource storage.
    """
    if not protocol_store.has(protocolId):
        raise ProtocolNotFound(detail=f"Protocol {protocolId} not found").as_error(
            status.HTTP_404_NOT_FOUND
        )

    try:
        # TODO(mm, 2022-04-28): This will erroneously return an analysis even if
        # this analysis isn't owned by this protocol. This should be an error.
        analysis = await analysis_store.get(analysisId)
    except AnalysisNotFoundError as error:
        raise AnalysisNotFound(detail=str(error)).as_error(
            status.HTTP_404_NOT_FOUND
        ) from error

    return await PydanticResponse.create(content=SimpleBody.construct(data=analysis))


@protocols_router.get(
    path="/protocols/{protocolId}/analyses/{analysisId}/asDocument",
    summary="[Experimental] Get one of a protocol's analyses as a raw document",
    description=(
        "**Warning:** This endpoint is experimental. We may change or remove it without warning."
        "\n\n"
        "This is a faster alternative to `GET /protocols/{protocolId}/analyses`"
        " and `GET /protocols/{protocolId}/analyses/{analysisId}`."
        " For large analyses (10k+ commands), those other endpoints can take minutes to respond,"
        " whereas this one should only take a few seconds."
        "\n\n"
        "For a completed analysis, this returns the same JSON data as the"
        " `GET /protocols/:id/analyses/:id` endpoint, except that it's not wrapped in a top-level"
        " `data` object."
        "\n\n"
        "For a *pending* analysis, this returns a 404 response, unlike those other"
        ' endpoints, which return a 200 response with `"status": "pending"`.'
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorBody[Union[ProtocolNotFound, AnalysisNotFound]]
        },
    },
)
async def get_protocol_analysis_as_document(
    protocolId: str,
    analysisId: str,
    protocol_store: ProtocolStore = Depends(get_protocol_store),
    analysis_store: AnalysisStore = Depends(get_analysis_store),
) -> PlainTextResponse:
    """Get a protocol analysis by analysis ID.

    Arguments:
        protocolId: The ID of the protocol, pulled from the URL.
        analysisId: The ID of the analysis, pulled from the URL.
        protocol_store: Protocol resource storage.
        analysis_store: Analysis resource storage.
    """
    if not protocol_store.has(protocolId):
        raise ProtocolNotFound(detail=f"Protocol {protocolId} not found").as_error(
            status.HTTP_404_NOT_FOUND
        )

    try:
        # TODO(mm, 2022-04-28): This will erroneously return an analysis even if
        # this analysis isn't owned by this protocol. This should be an error.
        analysis = await analysis_store.get_as_document(analysisId)
    except AnalysisNotFoundError as error:
        raise AnalysisNotFound(detail=str(error)).as_error(
            status.HTTP_404_NOT_FOUND
        ) from error

    return PlainTextResponse(content=analysis, media_type="application/json")
