from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Type

import api_logging as logging
from django.conf import settings
from django.db import models
from rest_framework_api_key.models import AbstractAPIKey
from scorer_weighted.models import BinaryWeightedScorer, Scorer, WeightedScorer

from .deduplication import Rules

log = logging.getLogger(__name__)

Q = models.Q
tz = timezone.utc


class EthAddressField(models.CharField):
    def __init__(self, *args, **kwargs):
        if "max_length" not in kwargs:
            kwargs["max_length"] = 42
        super().__init__(*args, **kwargs)

    def get_prep_value(self, value):
        return str(value).lower()


class Nonce(models.Model):
    nonce = models.CharField(
        max_length=60, blank=False, null=False, unique=True, db_index=True
    )
    created_on = models.DateTimeField(auto_now_add=True)
    expires_on = models.DateTimeField(null=True, blank=True)
    was_used = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.nonce} - used={self.was_used} - expires_on={self.expires_on}"

    def mark_as_used(self):
        self.was_used = True
        self.save()

    @classmethod
    def create_nonce(cls: Type[Nonce], ttl: Optional[int] = None) -> Nonce:
        expires_on = datetime.now(tz) + timedelta(seconds=ttl) if ttl else None

        nonce = Nonce(nonce=secrets.token_hex(30), expires_on=expires_on)
        nonce.save()

        return nonce

    @classmethod
    def validate_nonce(cls: Type[Nonce], nonce: str) -> Nonce:
        try:
            log.debug("Checking nonce: %s", nonce)
            return Nonce.objects.filter(
                Q(nonce=nonce),
                (Q(expires_on__isnull=True) | Q(expires_on__gt=datetime.now(tz))),
                Q(was_used=False),
            ).get()
        except Nonce.DoesNotExist:
            # Re-raise the exception
            raise

    @classmethod
    async def avalidate_nonce(cls: Type[Nonce], nonce: str) -> Nonce:
        try:
            log.debug("Checking nonce: %s", nonce)
            return await Nonce.objects.filter(
                Q(nonce=nonce),
                (Q(expires_on__isnull=True) | Q(expires_on__gt=datetime.now(tz))),
                Q(was_used=False),
            ).aget()
        except Nonce.DoesNotExist:
            # Re-raise the exception
            raise

    @classmethod
    def use_nonce(cls: Type[Nonce], nonce: str):
        try:
            nonceRecord = cls.validate_nonce(nonce)
            nonceRecord.was_used = True
            nonceRecord.save()
            return True
        except Exception:
            log.error("Error when validating nonce", exc_info=True)
            return False

    @classmethod
    async def ause_nonce(cls: Type[Nonce], nonce: str):
        try:
            nonceRecord = await cls.avalidate_nonce(nonce)
            nonceRecord.was_used = True
            await nonceRecord.asave()
            return True
        except Exception:
            log.error("Error when validating nonce", exc_info=True)
            return False


class RateLimits(str, Enum):
    TIER_1 = "125/15m"
    TIER_2 = "350/15m"
    TIER_3 = "2000/15m"
    UNLIMITED = ""

    def __str__(self):
        return f"{self.name} - {self.value}"


class Account(models.Model):
    address = EthAddressField(max_length=100, blank=False, null=False, db_index=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="account"
    )

    def __str__(self):
        return f"Account #{self.id} - {self.address} - {self.user_id}"


class AccountAPIKey(AbstractAPIKey):
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="api_keys", default=None
    )

    rate_limit = models.CharField(
        max_length=20,
        choices=[(limit.value, limit) for limit in RateLimits],
        default=RateLimits.TIER_1.value,
        null=True,
        blank=True,
    )

    submit_passports = models.BooleanField(default=True)
    read_scores = models.BooleanField(default=True)
    create_scorers = models.BooleanField(default=False)

    def rate_limit_display(self):
        if self.rate_limit == "" or self.rate_limit is None:
            return "Unlimited"
        return str(RateLimits(self.rate_limit))


class AccountAPIKeyAnalytics(models.Model):
    api_key = models.ForeignKey(
        AccountAPIKey, on_delete=models.CASCADE, related_name="analytics"
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        null=True,
        blank=True,
        db_index=True,
    )
    path = models.CharField(max_length=1000, blank=False, null=False, default="/")
    base_path = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        db_index=True,
    )

    path_segments = models.JSONField(
        help_text="Contains the parameters that are passed in the path segments",
        default=list,
        null=True,
        db_index=True,
    )

    query_params = models.JSONField(
        help_text="Contains the parameters that are passed in the query (get) parameters",
        null=True,
        db_index=True,
    )
    payload = models.JSONField(
        help_text="Contains the payload sent as part of the HTTP request body",
        null=True,
        db_index=True,
    )
    headers = models.JSONField(
        help_text="Contains the HTTP headers from the request", null=True
    )
    response = models.JSONField(
        help_text="Contains the response, unless response_skipped is set to True",
        null=True,
    )
    response_skipped = models.BooleanField(
        help_text="Indicates that the response response is not filled in this record. This will typically happen for large responses (when reading pages of data)",
        null=True,
        db_index=True,
    )
    error = models.TextField(help_text="Error that occured", null=True, db_index=True)


def get_default_community_scorer():
    """Returns the default scorer that shall be used for communities"""
    ws = WeightedScorer()
    ws.save()
    return ws


class Community(models.Model):
    class Meta:
        verbose_name_plural = "Communities"
        constraints = [
            # UniqueConstraint for non-deleted records
            models.UniqueConstraint(
                fields=["account", "name"],
                name="unique_non_deleted_name_per_account",
                condition=Q(deleted_at__isnull=True),
            ),
            # UniqueConstraint for deleted records
            models.UniqueConstraint(
                fields=["account", "name", "deleted_at"],
                name="unique_deleted_name_per_account",
                condition=Q(deleted_at__isnull=False),
            ),
        ]

    name = models.CharField(max_length=100, blank=False, null=False)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    rule = models.CharField(
        max_length=100,
        blank=False,
        null=False,
        default=Rules.LIFO.value,
        choices=Rules.choices(),
    )
    description = models.CharField(
        max_length=100, blank=False, null=False, default="My community"
    )
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="community", default=None
    )
    scorer = models.ForeignKey(
        Scorer, on_delete=models.CASCADE, default=get_default_community_scorer
    )
    use_case = models.CharField(
        blank=True,
        null=True,
        max_length=100,
        help_text="The use case that the creator of this community (Scorer) would like to cover",
    )

    external_scorer_id = models.CharField(
        max_length=42, unique=True, null=True, blank=True
    )

    def __repr__(self):
        return f"<Community {self.name}>"

    def __str__(self):
        return f"Community - #{self.id}, name={self.name}, account_id={self.account_id}"

    def get_scorer(self) -> Scorer:
        if self.scorer.type == Scorer.Type.WEIGHTED:
            return self.scorer.weightedscorer
        elif self.scorer.type == Scorer.Type.WEIGHTED_BINARY:
            return self.scorer.binaryweightedscorer

    async def aget_scorer(self) -> Scorer:
        scorer = await Scorer.objects.aget(pk=self.scorer_id)
        if scorer.type == Scorer.Type.WEIGHTED:
            return await WeightedScorer.objects.aget(scorer_ptr_id=scorer.id)
        elif scorer.type == Scorer.Type.WEIGHTED_BINARY:
            return await BinaryWeightedScorer.objects.aget(scorer_ptr_id=scorer.id)
