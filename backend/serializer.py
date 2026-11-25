"""Custom WebSocket frame serializer for browser PCM audio and JSON UI events."""

from __future__ import annotations

import json
from typing import Any

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.serializers.base_serializer import FrameSerializer


class BrowserAudioSerializer(FrameSerializer):
    """Bidirectional serializer between raw browser 16kHz PCM audio + JSON events and Pipecat frames."""

    def __init__(self, sample_rate: int = 16000, num_channels: int = 1) -> None:
        super().__init__(
            params=FrameSerializer.InputParams(
                ignore_rtvi_messages=True,
                audio_in_sample_rate=sample_rate,
                audio_out_sample_rate=sample_rate,
            )
        )
        self._wire_sample_rate = sample_rate
        self._sample_rate = sample_rate
        self._num_channels = num_channels

    async def setup(self, frame: FrameProcessorSetup) -> None:
        await super().setup(frame)
        if not self._sample_rate:
            self._sample_rate = self._wire_sample_rate

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            audio = await self._resample_audio_out(frame)
            return bytes(audio)

        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            if self.should_ignore_frame(frame):
                return None
            return json.dumps(frame.message)

        if isinstance(frame, InterruptionFrame):
            await self._reset_audio_out()
            return json.dumps({"type": "interruption"})

        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)):
            raw = bytes(data)
            if not raw:
                return None
            resampled = await self._resample_audio_in(raw)
            return InputAudioRawFrame(
                audio=resampled,
                sample_rate=self._sample_rate or self._wire_sample_rate,
                num_channels=self._num_channels,
            )

        if isinstance(data, str):
            try:
                payload: Any = json.loads(data)
                if isinstance(payload, dict):
                    return InputTransportMessageFrame(message=payload)
            except json.JSONDecodeError:
                return None

        return None
