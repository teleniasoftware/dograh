"""TVox (Native SIP) telephony configuration schemas."""

from typing import Literal

from pydantic import BaseModel, Field


class SIPConfigurationRequest(BaseModel):
    """Request schema for TVox native SIP ingress configuration."""

    provider: Literal["sip"] = Field(default="sip")

    host: str = Field(
        default="0.0.0.0",
        description="SIP server bind address",
    )
    port: int = Field(
        default=5090,
        ge=1,
        le=65535,
        description="SIP server UDP port",
    )
    rtp_start_port: int = Field(
        default=10000,
        ge=1024,
        le=65535,
        description="Start of RTP port range",
    )
    rtp_end_port: int = Field(
        default=10400,
        ge=1024,
        le=65535,
        description="End of RTP port range",
    )
    max_concurrent_calls: int = Field(
        default=100,
        ge=1,
        le=10000,
        description="Maximum concurrent SIP calls",
    )
    auth_username: str = Field(
        default="",
        description="SIP authentication username (optional)",
    )
    auth_password: str = Field(
        default="",
        description="SIP authentication password (optional)",
    )


class SIPConfigurationResponse(BaseModel):
    """Response schema for TVox configuration with masked sensitive fields."""

    provider: Literal["sip"] = Field(default="sip")
    host: str
    port: int
    rtp_start_port: int
    rtp_end_port: int
    max_concurrent_calls: int
    auth_username: str
    auth_password: str  # Masked by the API layer
