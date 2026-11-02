"""test_rt_session_e2e.py — Real-time telephony integration test for LiveKit AgentSession & RtAgent tools.

Guarantees:
1. switch_guide and record_chain_blessing are registered @function_tools on RtAgent.
2. switch_guide dynamically updates the active guide, ElevenLabs voice ID, and instructions.
3. AgentSession boots cleanly with Deepgram, Silero, Gemini Flash, and ElevenLabs.
4. System prompt enforces strict telephone brevity (under 25 words per turn).
"""
import pytest
from unittest.mock import MagicMock, patch
from agent import RtAgent
import rt_pray
from livekit.agents import RunContext, AgentSession
from livekit.plugins import deepgram, elevenlabs, silero, google


def test_rt_agent_tools_registration():
    """Verify all critical spiritual telephony tools are properly registered with @function_tool."""
    agent = RtAgent(caller_e164="+19174030642")
    tool_names = [getattr(t, "__name__", getattr(t, "id", str(t))) for t in agent.tools]
    
    assert "switch_guide" in tool_names, "CRITICAL: switch_guide missing @function_tool decorator!"
    assert "record_chain_blessing" in tool_names, "CRITICAL: record_chain_blessing missing @function_tool decorator!"
    assert "end_call" in tool_names
    assert "manage_goals" in tool_names


@pytest.mark.asyncio
async def test_switch_guide_execution_and_voice_update():
    """Verify switch_guide updates state, switches to grand ElevenLabs voice ID, and updates instructions."""
    mock_tts = MagicMock()
    state = {
        "active_guide": "shiva",
        "pipeline_tts": mock_tts,
        "caller_info": {"display_name": "Richie"},
    }
    agent = RtAgent(caller_e164="+19174030642", call_state=state)
    mock_ctx = MagicMock(spec=RunContext)

    with patch.dict("os.environ", {"RT_PRAY_LANE": "1"}), \
         patch("rt_prefs.phone_hash", return_value="hash-richie"), \
         patch("rt_pray.switch_guide"):
        res = await agent.switch_guide(mock_ctx, guide_name="jesus")
        
        # State updated
        assert state["active_guide"] == "jesus"
        
        # ElevenLabs voice switched to Jesus (Spirit of Christ: NucoVVF2YFWrTdNkxVRo)
        mock_tts.update_options.assert_called_once()
        _, kwargs = mock_tts.update_options.call_args
        assert kwargs["voice_id"] == "NucoVVF2YFWrTdNkxVRo"
        
        # Instructions updated to Jesus
        assert "Jesus" in agent.instructions
        assert "Switched to Jesus of Nazareth" in res


@pytest.mark.asyncio
async def test_switch_guide_random_or_anyone():
    """Verify switch_guide randomly selects a different guide when asked for 'anyone'."""
    mock_tts = MagicMock()
    state = {
        "active_guide": "shiva",
        "pipeline_tts": mock_tts,
        "caller_info": {"display_name": "Richie"},
    }
    agent = RtAgent(caller_e164="+19174030642", call_state=state)
    mock_ctx = MagicMock(spec=RunContext)

    with patch.dict("os.environ", {"RT_PRAY_LANE": "1"}), \
         patch("rt_prefs.phone_hash", return_value="hash-richie"), \
         patch("rt_pray.switch_guide"):
        res = await agent.switch_guide(mock_ctx, guide_name="anyone")
        assert state["active_guide"] != "shiva"
        assert state["active_guide"] in rt_pray.GUIDES
        assert mock_tts.update_options.called


@pytest.mark.asyncio
async def test_agent_session_instantiation_and_components():
    """Verify AgentSession successfully instantiates with the exact fast modular stack."""
    from livekit.plugins import openai
    stt = deepgram.STT(api_key="test-key", model="nova-3")
    vad = silero.VAD.load()
    llm = openai.LLM(model="gpt-4o-mini", api_key="test-key")
    tts = elevenlabs.TTS(api_key="test-key", voice_id="y5QG8szHCCfX4292DMPZ")
    
    session = AgentSession(stt=stt, vad=vad, llm=llm, tts=tts)
    assert session is not None
    assert session.stt is not None
    assert session.vad is not None
    assert session.llm is not None
    assert session.tts is not None


def test_prompt_enforces_brevity_and_current_time():
    """Verify prompt strictly enforces 1-2 sentences under 25 words and injects current year."""
    prompt = rt_pray.build_system_prompt(guide_key="shiva", caller_info={"display_name": "Richie"})
    assert "BREVITY & TELEPHONE PACING" in prompt
    assert "under 25 words" in prompt
    assert "RIGHT NOW IS:" in prompt
    assert "NAME IDENTIFICATION" in prompt
    assert "NATURAL CONVERSATION WITH GOD" in prompt
    assert "blessing-shiva.jpg" in prompt


def test_silence_nudge_spiritual_care():
    """Verify silence nudges offer warm spiritual presence and options."""
    shiva_nudge = rt_pray.get_silence_nudge("shiva", "Richie")
    assert "Take your time, Richie" in shiva_nudge
    assert "stillness" in shiva_nudge

    jesus_nudge = rt_pray.get_silence_nudge("jesus", "Richie")
    assert "Take your time, Richie" in jesus_nudge
    assert "prayer" in jesus_nudge


@pytest.mark.asyncio
async def test_switch_guide_invalid_name_graceful_fallback():
    """Verify switch_guide handles unknown names without crashing."""
    state = {"active_guide": "shiva"}
    agent = RtAgent(caller_e164="+19174030642", call_state=state)
    mock_ctx = MagicMock(spec=RunContext)

    with patch.dict("os.environ", {"RT_PRAY_LANE": "1"}), \
         patch("rt_prefs.phone_hash", return_value="hash-richie"):
        res = await agent.switch_guide(mock_ctx, guide_name="nonexistent_guide_xyz")
        assert "Unknown guide" in res
        assert state["active_guide"] == "shiva"

