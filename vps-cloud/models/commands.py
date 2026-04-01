"""models/commands.py – Pydantic models for IoT device commands."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class DeviceCommand(BaseModel):
    """Represents a command to be executed by the local-edge agent."""

    device_type: Literal["switch", "pishock", "audio", "lovense"]
    action: str
    duration: Optional[int] = Field(
        None, ge=1, description="Activation duration in seconds."
    )
