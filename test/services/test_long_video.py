import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

import cli
from app.models import const
from app.models.schema import TaskVideoRequest, VideoAspect, VideoParams
from app.services import llm, long_video as lv, task as tm
from app.services.state import MemoryState


@pytest.mark.parametrize("minutes", [0, 10, 20, 30])
def test_duration_contract(minutes):
    params = TaskVideoRequest(video_subject="Rome", target_duration_minutes=minutes)
    assert params.video_aspect == (
        VideoAspect.landscape if minutes else VideoAspect.portrait
    )


@pytest.mark.parametrize("minutes", [-1, 1, 9, 31, 10.5, True, "10"])
def test_invalid_duration(minutes):
    with pytest.raises(ValidationError):
        VideoParams(video_subject="Rome", target_duration_minutes=minutes)


@pytest.mark.parametrize(
    "options",
    [
        {"video_count": 2},
        {"custom_audio_file": "voice.mp3"},
        {"video_source": "loomloom"},
        {"voice_rate": 0},
        {"voice_rate": float("nan")},
    ],
)
def test_unsupported_long_combinations_fail_before_work(options):
    with pytest.raises(ValidationError):
        VideoParams(video_subject="Rome", target_duration_minutes=10, **options)
    if "voice_rate" not in options:
        assert VideoParams(video_subject="Rome", **options).target_duration_minutes == 0


def test_cli_option_reaches_shared_model():
    args = cli.parse_args(
        ["--video-subject", "Rome", "--target-duration-minutes", "20"]
    )
    params = cli.build_video_params(args)
    assert params.target_duration_minutes == 20
    assert params.video_aspect == VideoAspect.landscape


@pytest.mark.parametrize("minutes, status", [(10, 200), (9, 422)])
def test_api_validates_and_queues_long_mode(monkeypatch, minutes, status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.controllers import base
    from app.controllers.v1 import video as controller

    application = FastAPI()
    application.include_router(controller.router)
    application.dependency_overrides[base.verify_token] = lambda: None
    queue = Mock()
    monkeypatch.setattr(controller.task_manager, "add_task", queue)
    monkeypatch.setattr(controller.sm, "state", MemoryState())
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/videos",
            headers={"x-task-id": "request-long"},
            json={"video_subject": "Rome", "target_duration_minutes": minutes},
        )
    assert response.status_code == status
    if status == 200:
        params = queue.call_args.kwargs["params"]
        assert params.target_duration_minutes == 10
        assert params.video_aspect == VideoAspect.landscape
    else:
        queue.assert_not_called()


@pytest.mark.parametrize(
    "script",
    [
        "First paragraph.\n\n" + "Another detailed sentence! " * 500,
        "罗马历史。" * 2000,
        "x" * 9000,
        "",
        "One sentence.",
    ],
)
def test_split_never_loses_text_or_exceeds_provider_block_limit(script):
    blocks = lv.split_script(script)
    assert "".join("".join(block.split()) for block in blocks) == "".join(
        script.split()
    )
    assert all(0 < len(block) <= lv.MAX_BLOCK_CHARACTERS for block in blocks)
    assert all(len(block.split()) <= lv.MAX_BLOCK_WORDS for block in blocks)


def test_long_script_uses_existing_dispatcher_and_narrative_context(monkeypatch):
    titles = [f"Chapter {i}" for i in range(5)]
    narration = "A detailed historical fact. " * 70
    generate = Mock(side_effect=[json.dumps(titles), *[narration] * 5])
    monkeypatch.setattr(llm, "_generate_response", generate)
    chapters = llm.generate_long_script(
        "Rome", 10, "pt-BR", video_script_prompt="Be concrete"
    )
    assert len(chapters) == 5
    prompts = [call.kwargs["prompt"] for call in generate.call_args_list]
    assert all("pt-BR" in p and "Be concrete" in p for p in prompts)
    assert "280 spoken words" in prompts[1]
    assert narration.strip()[-1000:] in prompts[2]


def test_bad_outline_is_retried_and_never_reaches_narration(monkeypatch):
    generate = Mock(return_value='["Only one"]')
    monkeypatch.setattr(llm, "_generate_response", generate)
    with pytest.raises(ValueError, match="outline"):
        llm.generate_long_script("Rome", 10)
    assert generate.call_count == llm._max_retries


def test_short_generated_chapter_is_not_silently_accepted(monkeypatch):
    generate = Mock(
        side_effect=[
            json.dumps([str(i) for i in range(5)]),
            *["Too short."] * llm._max_retries,
        ]
    )
    monkeypatch.setattr(llm, "_generate_response", generate)
    with pytest.raises(ValueError, match="too short"):
        llm.generate_long_script("Rome", 10)


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    def task_dir(task_id=""):
        directory = tmp_path / task_id
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)

    monkeypatch.setattr(tm.utils, "task_dir", task_dir)
    state = MemoryState()
    monkeypatch.setattr(tm.sm, "state", state)
    monkeypatch.setattr(tm.utils, "check_ffmpeg_ready", lambda: True)
    monkeypatch.setattr(lv, "MAX_BLOCK_WORDS", 4)
    events = []

    def terms(task_id, params, script):
        events.append(("terms", task_id, script))
        return [script.split()[0]]

    def audio(task_id, params, script):
        events.append(("audio", task_id, script))
        return str(Path(task_dir(task_id)) / "audio.mp3"), 300, object()

    def materials(task_id, params, terms, duration):
        events.append(("materials", task_id, terms))
        assert duration == 300
        return ["stock.mp4"]

    def render(
        task_id, params, materials, audio_file, subtitle, duration, progress_callback
    ):
        events.append(("video", task_id, params.video_script))
        progress_callback(100)
        output = Path(task_dir(task_id)) / "final-1.mp4"
        output.write_bytes(b"test")
        return [str(output)], [str(output.with_name("combined-1.mp4"))], []

    monkeypatch.setattr(tm, "generate_terms", terms)
    monkeypatch.setattr(tm, "generate_audio", audio)
    monkeypatch.setattr(tm.voice, "get_audio_duration", lambda _: 300)
    monkeypatch.setattr(tm, "generate_subtitle", lambda *args: "chapter.srt")
    monkeypatch.setattr(tm, "get_video_materials", materials)
    monkeypatch.setattr(tm, "generate_final_videos", render)
    concat = Mock(side_effect=lambda inputs, output: Path(output).write_bytes(b"final"))
    monkeypatch.setattr(lv.video, "concat_chapters_with_ffmpeg", concat)
    publish = Mock()
    monkeypatch.setattr(tm, "_schedule_cross_post", publish)
    params = VideoParams(
        video_subject="Rome",
        video_script="Opening of the story. Ending of the story.",
        target_duration_minutes=10,
        bgm_type="",
        subtitle_enabled=False,
    )
    return params, events, state, concat, publish


def test_chapters_are_sequential_isolated_and_concatenated_once(pipeline, tmp_path):
    params, events, state, concat, publish = pipeline
    result = tm.start("parent", params)
    assert [e[0] for e in events] == ["terms", "audio", "materials", "video"] * 2
    assert events[0][1] != events[4][1]
    assert events[0][2].startswith("Opening") and events[4][2].startswith("Ending")
    assert result["audio_duration"] == 600
    assert len(result["videos"]) == 1
    assert len(concat.call_args.args[0]) == 2
    assert all(ch["status"] == "complete" for ch in result["chapters"])
    assert state.get_task("parent")["state"] == const.TASK_STATE_COMPLETE
    assert state.get_all_tasks(1, 100)[1] == 1
    manifest = json.loads((tmp_path / "parent/chapters.json").read_text())
    assert manifest["completed_stage"] == "video"
    assert params.target_duration_minutes == 10
    publish.assert_not_called()


@pytest.mark.parametrize("stage", lv.STAGES[:-1])
def test_stop_at_does_not_execute_later_stages(pipeline, stage):
    params, events, state, concat, publish = pipeline
    result = tm.start("parent", params, stop_at=stage)
    assert state.get_task("parent")["state"] == const.TASK_STATE_COMPLETE
    assert "videos" not in result
    assert all(lv.STAGES.index(event[0]) <= lv.STAGES.index(stage) for event in events)
    concat.assert_not_called()
    publish.assert_not_called()


def test_failure_preserves_first_chapter_and_reports_second(
    pipeline, monkeypatch, tmp_path
):
    params, _, state, concat, _ = pipeline
    original = tm.generate_audio

    def fail_second(task_id, *args):
        if task_id.endswith("002"):
            tm._mark_task_failed(
                task_id,
                "audio",
                "provider timed out",
                details={"remote_job_id": "job-123"},
            )
            return None, None, None
        return original(task_id, *args)

    monkeypatch.setattr(tm, "generate_audio", fail_second)
    result = tm.start("parent", params)
    assert result["failed_stage"] == "audio"
    assert result["failed_chapter"] == 2
    assert "provider timed out" in result["error"]
    assert result["chapter_failure"]["remote_job_id"] == "job-123"
    assert (tmp_path / "parent/chapters/001/final-1.mp4").exists()
    assert state.get_task("parent")["state"] == const.TASK_STATE_FAILED
    assert state.get_all_tasks(1, 100)[1] == 1
    concat.assert_not_called()


def test_concat_failure_never_marks_task_complete(pipeline):
    params, _, state, concat, _ = pipeline
    concat.side_effect = RuntimeError("disk full")
    result = tm.start("parent", params)
    assert result["failed_stage"] == "concat"
    assert result["failed_chapter"] is None
    assert state.get_task("parent")["state"] == const.TASK_STATE_FAILED


def test_short_pipeline_remains_default(pipeline, monkeypatch):
    params, _, _, _, _ = pipeline
    params.target_duration_minutes = 0
    long = Mock()
    monkeypatch.setattr(lv, "run_long_video", long)
    assert tm.start("short", params, stop_at="script")["script"] == params.video_script
    long.assert_not_called()


def test_mutated_params_are_revalidated_before_long_generation(pipeline):
    params, events, _, concat, _ = pipeline
    params.custom_audio_file = "unexpected.mp3"
    result = tm.start("parent", params)
    assert result["failed_stage"] == "preflight"
    assert not events
    concat.assert_not_called()
