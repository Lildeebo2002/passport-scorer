from datetime import datetime
from typing import List, Optional

import api_logging as logging

# --- Deduplication Modules
from account.models import Account, Community
from django.db.models import Max, Q
from ninja import Router
from registry.api import v1
from registry.api.v1 import with_read_db
from registry.models import Event, Score
from registry.utils import (
    decode_cursor,
    encode_cursor,
    get_cursor_query_condition,
    get_cursor_tokens_for_results,
    reverse_lazy_with_query,
)

from ..exceptions import (
    InvalidAPIKeyPermissions,
    InvalidCommunityScoreRequestException,
    InvalidLimitException,
    NotFoundApiException,
    api_get_object_or_404,
)
from .base import ApiKey, check_rate_limit
from .schema import (
    CursorPaginatedHistoricalScoreResponse,
    CursorPaginatedScoreResponse,
    CursorPaginatedStampCredentialResponse,
    DetailedHistoricalScoreResponse,
    DetailedScoreResponse,
    ErrorMessageResponse,
    SigningMessageResponse,
    StampDisplayResponse,
    SubmitPassportPayload,
)

log = logging.getLogger(__name__)

router = Router()

analytics_router = Router()


@router.get(
    "/signing-message",
    auth=ApiKey(),
    response={
        200: SigningMessageResponse,
        401: ErrorMessageResponse,
        400: ErrorMessageResponse,
    },
    summary="Submit passport for scoring",
    description="""Use this API to get a message to sign and a nonce to use when submitting your passport for scoring.""",
)
def signing_message(request) -> SigningMessageResponse:
    return v1.signing_message(request)


@router.post(
    "/submit-passport",
    auth=v1.aapi_key,
    response={
        200: DetailedScoreResponse,
        401: ErrorMessageResponse,
        400: ErrorMessageResponse,
        404: ErrorMessageResponse,
    },
    summary="Submit passport for scoring",
    description="""Use this API to submit your passport for scoring.\n
This API will return a `DetailedScoreResponse` structure with status **PROCESSING** immediatly while your passport is being pulled from storage and the scoring algorithm is run.\n
You need to check for the status of the operation by calling the `/score/{int:scorer_id}/{str:address}` API. The operation will have finished when the status returned is **DONE**
""",
)
async def a_submit_passport(
    request, payload: SubmitPassportPayload
) -> DetailedScoreResponse:
    return await v1.a_submit_passport(request, payload)


@router.get(
    "/score/{int:scorer_id}",
    auth=ApiKey(),
    response={
        200: CursorPaginatedScoreResponse,
        401: ErrorMessageResponse,
        400: ErrorMessageResponse,
        404: ErrorMessageResponse,
    },
    summary="Get scores for all addresses that are associated with a scorer",
    description="""Use this endpoint to fetch the scores for all addresses that are associated with a scorer\n
This endpoint will return a `CursorPaginatedScoreResponse`.\n
\n

Note: results will be sorted ascending by `["last_score_timestamp", "id"]`
""",
)
def get_scores(
    request,
    scorer_id: int,
    address: Optional[str] = None,
    last_score_timestamp__gt: str = "",
    last_score_timestamp__gte: str = "",
    token: str = None,
    limit: int = 1000,
) -> CursorPaginatedScoreResponse:
    check_rate_limit(request)

    if limit > 1000:
        raise InvalidLimitException()

    # Get community object
    user_community = api_get_object_or_404(
        Community, id=scorer_id, account=request.auth
    )
    try:
        base_query = (
            with_read_db(Score)
            .filter(passport__community__id=user_community.id)
            .select_related("passport")
        )

        filter_condition = Q()
        field_ordering = None
        cursor = decode_cursor(token) if token else None
        sort_fields = ["last_score_timestamp", "id"]

        if cursor:
            cursor = decode_cursor(token)
            cursor["last_score_timestamp"] = datetime.fromisoformat(
                cursor.get("last_score_timestamp")
            )
            filter_condition, field_ordering = get_cursor_query_condition(
                cursor, sort_fields
            )

        else:
            field_ordering = ["last_score_timestamp", "id"]

            if address:
                filter_condition &= Q(passport__address=address)

            if last_score_timestamp__gt:
                filter_condition &= Q(last_score_timestamp__gt=last_score_timestamp__gt)

            if last_score_timestamp__gte:
                filter_condition &= Q(
                    last_score_timestamp__gte=last_score_timestamp__gte
                )

        has_more_scores = has_prev_scores = False
        next_cursor = prev_cursor = {}

        query = base_query.filter(filter_condition).order_by(*field_ordering)
        scores = query[:limit]
        scores = list(scores)

        if cursor and cursor["d"] == "prev":
            scores.reverse()

        if scores:
            next_id = scores[-1].id
            next_lts = scores[-1].last_score_timestamp
            prev_id = scores[0].id
            prev_lts = scores[0].last_score_timestamp

            next_cursor = dict(
                d="next",
                id=next_id,
                last_score_timestamp=next_lts.isoformat(),
                address=address,
                last_score_timestamp__gt=last_score_timestamp__gt,
                last_score_timestamp__gte=last_score_timestamp__gte,
            )
            prev_cursor = dict(
                d="prev",
                id=prev_id,
                last_score_timestamp=prev_lts.isoformat(),
                address=address,
                last_score_timestamp__gt=last_score_timestamp__gt,
                last_score_timestamp__gte=last_score_timestamp__gte,
            )

            next_filter_cond, _ = get_cursor_query_condition(next_cursor, sort_fields)
            prev_filter_cond, _ = get_cursor_query_condition(prev_cursor, sort_fields)

            has_more_scores = base_query.filter(next_filter_cond).exists()
            has_prev_scores = base_query.filter(prev_filter_cond).exists()

        domain = request.build_absolute_uri("/")[:-1]

        next_url = (
            f"""{domain}{reverse_lazy_with_query(
                "registry_v2:get_scores",
                args=[scorer_id],
                query_kwargs={"token": encode_cursor(**next_cursor), "limit": limit},
            )}"""
            if has_more_scores
            else None
        )

        prev_url = (
            f"""{domain}{reverse_lazy_with_query(
                "registry_v2:get_scores",
                args=[scorer_id],
                query_kwargs={"token": encode_cursor(**prev_cursor), "limit": limit},
            )}"""
            if has_prev_scores
            else None
        )

        response = CursorPaginatedScoreResponse(
            next=next_url, prev=prev_url, items=scores
        )

        return response

    except Exception as e:
        log.error(
            "Error getting passport scores. scorer_id=%s",
            scorer_id,
            exc_info=True,
        )
        raise e


@router.get(
    "/stamps/{str:address}",
    auth=ApiKey(),
    response={
        200: CursorPaginatedStampCredentialResponse,
        400: ErrorMessageResponse,
        401: ErrorMessageResponse,
    },
    summary="Get passport for an address",
    description="""Use this endpoint to fetch the passport for a specific address\n
This endpoint will return a `CursorPaginatedStampCredentialResponse`.\n
""",
    # This prevents returning {metadata: None} in the response
    exclude_unset=True,
)
def get_passport_stamps(
    request,
    address: str,
    token: str = "",
    limit: int = 1000,
    include_metadata: bool = False,
) -> CursorPaginatedStampCredentialResponse:
    return v1.get_passport_stamps(request, address, token, limit, include_metadata)


@router.get(
    "/stamp-metadata",
    description="""**WARNING**: This endpoint is in beta and is subject to change.""",
    auth=ApiKey(),
    response={
        200: List[StampDisplayResponse],
        500: ErrorMessageResponse,
    },
)
def stamp_display(request) -> List[StampDisplayResponse]:
    return v1.stamp_display(request)


@router.get(
    "/gtc-stake/{address}",
    description="Get self and community staking amounts based on address and round id",
    auth=ApiKey(),
    response=v1.GqlResponse,
)
def get_gtc_stake(request, address: str):
    return v1.get_gtc_stake(request, address)


@router.get(
    "/score/{int:scorer_id}/history",
    auth=ApiKey(),
    response={
        200: CursorPaginatedHistoricalScoreResponse,
        401: ErrorMessageResponse,
        400: ErrorMessageResponse,
        404: ErrorMessageResponse,
    },
    summary="Get score history based on timestamp and optional address that is associated with a scorer",
    description="""Use this endpoint to get historical Passport score history based on timestamp and optional user address\n
    This endpoint will return a `CursorPaginatedScoreResponse` that will include either a list of historical scores based on scorer ID and timestamp, or a single address representing the most recent score from the timestamp.\n
    \n

    Note: results will be sorted descending by `["created_at", "id"]`
    """,
)
def get_score_history(
    request,
    scorer_id: int,
    address: Optional[str] = None,
    created_at: str = "",
    token: str = None,
    limit: int = 1000,
) -> CursorPaginatedHistoricalScoreResponse:
    check_rate_limit(request)

    if limit > 1000:
        raise InvalidLimitException()

    community = api_get_object_or_404(Community, id=scorer_id, account=request.auth)

    try:
        base_query = with_read_db(Event).filter(
            community__id=community.id, action=Event.Action.SCORE_UPDATE
        )

        cursor = decode_cursor(token) if token else None

        if cursor and "created_at" in cursor:
            created_at = datetime.fromisoformat(cursor.get("created_at"))
        elif created_at:
            created_at = datetime.fromisoformat(created_at)
        else:
            created_at = None

        # Scenario 1 - Snapshot for 1 addresses
        # the user has passed in the created_at and address
        # In this case only 1 result will be returned
        if address and created_at:
            score = (
                base_query.filter(address=address, created_at__lte=created_at)
                .order_by("-created_at")
                .first()
            )

            score.created_at = score.created_at.isoformat()

            response = CursorPaginatedHistoricalScoreResponse(
                next=None, prev=None, items=[score]
            )
            return response

        # Scenario 2 - Snapshot for all addresses
        # the user has passed in the created_at, but no address
        elif created_at:
            pagination_sort_fields = ["address"]
            filter_condition, field_ordering = get_cursor_query_condition(
                cursor, pagination_sort_fields
            )

            field_ordering.insert(0, "address")
            field_ordering.append("-created_at")
            query = (
                base_query.filter(filter_condition)
                .order_by(*field_ordering)
                .distinct("address")
            )

            scores = list(query[:limit])
            for score in scores:
                score.created_at = score.created_at.isoformat()

            if cursor and cursor["d"] == "prev":
                scores.reverse()

            domain = request.build_absolute_uri("/")[:-1]

            page_links = get_cursor_tokens_for_results(
                base_query, domain, scores, pagination_sort_fields, limit, [scorer_id]
            )

            response = CursorPaginatedHistoricalScoreResponse(
                next=page_links["next"], prev=page_links["prev"], items=scores
            )

            return response
        # Scenario 3 - Just return history ...
        else:
            pagination_sort_fields = ["id"]
            filter_condition, field_ordering = get_cursor_query_condition(
                cursor, pagination_sort_fields
            )

            field_ordering.insert(0, "address")
            query = (
                base_query.filter(filter_condition)
                .order_by(*field_ordering)
                .distinct("address")
            )

            scores = list(query[:limit])
            for score in scores:
                score.created_at = score.created_at.isoformat()

            if cursor and cursor["d"] == "prev":
                scores.reverse()

            domain = request.build_absolute_uri("/")[:-1]

            page_links = get_cursor_tokens_for_results(
                base_query, domain, scores, pagination_sort_fields, limit, [scorer_id]
            )

            response = CursorPaginatedHistoricalScoreResponse(
                next=page_links["next"], prev=page_links["prev"], items=scores
            )
            return response

    except Exception as e:
        log.error(
            "Error getting passport scores. scorer_id=%s",
            scorer_id,
            exc_info=True,
        )
        raise e


@router.get(
    "/score/{int:scorer_id}/{str:address}",
    auth=ApiKey(),
    response={
        200: DetailedScoreResponse,
        401: ErrorMessageResponse,
        400: ErrorMessageResponse,
        404: ErrorMessageResponse,
    },
    summary="Get score for an address that is associated with a scorer",
    description=f"""Use this endpoint to fetch the score for a specific address that is associated with a scorer\n
This endpoint will return a `DetailedScoreResponse`. This endpoint will also return the status of the asynchronous operation that was initiated with a request to the `/submit-passport` API.\n
{v1.SCORE_TIMESTAMP_FIELD_DESCRIPTION}
""",
)
def get_score(request, address: str, scorer_id: int) -> DetailedScoreResponse:
    return v1.get_score(request, address, scorer_id)
