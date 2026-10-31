"""DiscordPyVoicePlayer against discord.py's real 20 ms sender thread, minus the network."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from discord.opus import OPUS_SILENCE
from discord.player import AudioPlayer

from minie.voice.base import VoiceError
from minie.voice.discordpy import DiscordPyVoicePlayer

GUILD = 1
FRAME = bytes([31 << 3 | 0b100]) + bytes(20)


class FakeVoiceClient:
    """Implements just what discord.py's AudioPlayer calls, recording the packets sent."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.sent: list[bytes] = []
        self.client = SimpleNamespace(loop=loop)
        self.ws = SimpleNamespace(speak=self._speak)
        self.channel = SimpleNamespace(id=2)
        self._player: AudioPlayer | None = None

    async def _speak(self, state: Any) -> None:
        pass

    def is_connected(self) -> bool:
        return True

    def send_audio_packet(self, data: bytes, *, encode: bool) -> None:
        assert not encode
        self.sent.append(data)

    def play(self, source: discord.AudioSource, *, after: Any) -> None:
        self._player = AudioPlayer(source, cast(discord.VoiceClient, self), after=after)
        self._player.start()

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()
            self._player = None


def run_with_fake_connection(monkeypatch: pytest.MonkeyPatch, scenario: Any) -> FakeVoiceClient:
    async def main() -> FakeVoiceClient:
        fake = FakeVoiceClient(asyncio.get_running_loop())
        player = DiscordPyVoicePlayer(cast(discord.Client, object()))
        monkeypatch.setattr(player, "_voice_client", lambda guild_id: fake)
        await scenario(player)
        return fake

    return asyncio.run(main())


def test_plays_every_frame_then_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(player: DiscordPyVoicePlayer) -> None:
        playback = await player.play(GUILD, [FRAME] * 5)
        assert player.is_playing(GUILD)
        await asyncio.wait_for(playback, timeout=2)
        assert not player.is_playing(GUILD)

    fake = run_with_fake_connection(monkeypatch, scenario)
    # discord.py ends every playback with five frames of silence.
    assert fake.sent == [FRAME] * 5 + [OPUS_SILENCE] * 5


def test_stop_interrupts_and_lets_the_next_sound_play(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(player: DiscordPyVoicePlayer) -> None:
        first = await player.play(GUILD, [FRAME] * 500)  # 10 seconds
        await asyncio.sleep(0.1)
        await player.stop(GUILD)
        assert first.done()
        await asyncio.wait_for(await player.play(GUILD, [FRAME] * 2), timeout=2)

    fake = run_with_fake_connection(monkeypatch, scenario)
    assert len(fake.sent) < 50


def test_refuses_to_play_over_a_running_sound(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(player: DiscordPyVoicePlayer) -> None:
        await player.play(GUILD, [FRAME] * 500)
        with pytest.raises(VoiceError, match="already playing"):
            await player.play(GUILD, [FRAME])
        await player.stop(GUILD)

    run_with_fake_connection(monkeypatch, scenario)
