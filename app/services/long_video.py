"""Opt-in chapter orchestration over the existing short-video services."""

import math
import re
from pathlib import Path

from app.models import const
from app.models.schema import VideoConcatMode
from app.services import llm, state as sm, task_artifacts, video, voice
from app.utils import utils

MAX_BLOCK_CHARACTERS = 2500
MAX_BLOCK_WORDS = 280
STAGES = ("script", "terms", "audio", "subtitle", "materials", "video")


def split_script(script: str) -> list[str]:
    """Bound each TTS request, preferring paragraphs/sentences over word cuts.

    No content is discarded; only whitespace at chapter boundaries is stripped.
    Character limits also cover unspaced text and exceptionally long tokens.
    """
    remaining = script.strip()
    blocks = []
    while remaining:
        limit = min(len(remaining), MAX_BLOCK_CHARACTERS)
        words = list(re.finditer(r"\S+", remaining[:limit]))
        if len(words) > MAX_BLOCK_WORDS:
            limit = words[MAX_BLOCK_WORDS].start()
        if limit < len(remaining):
            candidates = list(
                re.finditer(
                    r"\n\s*\n|[.!?。！？](?:\s+|(?=[^\x00-\x7f]))", remaining[:limit]
                )
            )
            if candidates and candidates[-1].end() >= limit // 2:
                limit = candidates[-1].end()
            else:
                space = remaining.rfind(" ", 0, limit + 1)
                if space >= limit // 2:
                    limit = space
        blocks.append(remaining[:limit].strip())
        remaining = remaining[limit:].strip()
    return blocks


def prepare_chapters(params) -> list[dict]:
    if params.video_script.strip():
        source = [{"title": "Narration", "script": params.video_script}]
    else:
        source = llm.generate_long_script(
            video_subject=params.video_subject,
            target_duration_minutes=params.target_duration_minutes,
            language=params.video_language,
            voice_rate=params.voice_rate,
            video_script_prompt=params.video_script_prompt,
            custom_system_prompt=params.custom_system_prompt,
        )
    chapters = []
    for chapter in source:
        blocks = split_script(chapter["script"])
        for part, block in enumerate(blocks, 1):
            title = chapter["title"]
            if len(blocks) > 1:
                title += f" ({part}/{len(blocks)})"
            chapters.append(
                {
                    "index": len(chapters) + 1,
                    "title": title,
                    "script": block,
                    "status": "pending",
                }
            )
    if not chapters:
        raise ValueError("long video script is empty")
    return chapters


def run_long_video(task_id, params, stop_at="video"):
    # Import only at execution time to avoid a task/long_video import cycle.
    from app.services import task as tm

    if stop_at not in STAGES:
        return tm._mark_task_failed(task_id, "preflight", "invalid long video stop_at")
    directory = Path(utils.task_dir(task_id))
    manifest_path = directory / "chapters.json"
    manifest = {
        "version": 1,
        "target_duration_minutes": params.target_duration_minutes,
        "video_aspect": "16:9",
        "status": "processing",
        "chapters": [],
    }
    stage = "script"
    active_chapter = None

    def save():
        task_artifacts._write_json_atomic(manifest_path, manifest)

    def complete(result):
        manifest["status"] = "complete"
        manifest["completed_stage"] = stop_at
        save()
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, **result
        )
        return result

    try:
        chapters = prepare_chapters(params)
        manifest["chapters"] = chapters
        script = "\n\n".join(chapter["script"] for chapter in chapters)
        tm.save_script_data(task_id, script, [], params)
        save()
        result = {
            "script": script,
            "chapters": chapters,
            "chapters_manifest": str(manifest_path),
            "target_duration_minutes": params.target_duration_minutes,
        }
        if stop_at == "script":
            return complete(result)

        for index, chapter in enumerate(chapters):
            active_chapter = chapter
            chapter_id = f"{task_id}/chapters/{index + 1:03d}"
            chapter_params = params.model_copy(
                deep=True,
                update={
                    "target_duration_minutes": 0,
                    "video_script": chapter["script"],
                    "video_concat_mode": VideoConcatMode(
                        params.video_concat_mode or "random"
                    ),
                },
            )

            # Chapter helpers use isolated paths. Only the parent appears in task
            # listings; temporary failure state is copied before it is removed.
            def progress(value, chapter_index=index):
                total = 10 + 80 * (chapter_index + value / 100) / len(chapters)
                sm.state.update_task(
                    task_id,
                    progress=int(total),
                    current_chapter=chapter_index + 1,
                    chapter_count=len(chapters),
                )

            def require(value, message):
                if not value:
                    failure = sm.state.get_task(chapter_id) or {}
                    raise RuntimeError(failure.get("error") or message)
                return value

            try:
                chapter["status"] = "processing"
                progress(0)
                stage = "terms"
                terms = (
                    []
                    if params.video_source == "local"
                    else require(
                        tm.generate_terms(
                            chapter_id, chapter_params, chapter["script"]
                        ),
                        "failed to generate chapter search terms",
                    )
                )
                chapter["terms"] = terms
                tm.save_script_data(
                    chapter_id, chapter["script"], terms, chapter_params
                )
                if stop_at != "terms":
                    stage = "audio"
                    audio_file, _, sub_maker = tm.generate_audio(
                        chapter_id, chapter_params, chapter["script"]
                    )
                    require(audio_file, "failed to generate chapter audio")
                    duration = voice.get_audio_duration(audio_file)
                    if not math.isfinite(duration) or duration <= 0:
                        raise ValueError("invalid chapter audio duration")
                    chapter.update(audio_file=audio_file, audio_duration=duration)
                    progress(20)
                if STAGES.index(stop_at) >= STAGES.index("subtitle"):
                    stage = "subtitle"
                    chapter["subtitle_path"] = tm.generate_subtitle(
                        chapter_id,
                        chapter_params,
                        chapter["script"],
                        sub_maker,
                        audio_file,
                    )
                    progress(30)
                if STAGES.index(stop_at) >= STAGES.index("materials"):
                    stage = "materials"
                    chapter["materials"] = require(
                        tm.get_video_materials(
                            chapter_id, chapter_params, terms, duration
                        ),
                        "failed to prepare chapter materials",
                    )
                    progress(40)
                if stop_at == "video":
                    stage = "video"
                    finals, combined, warnings = tm.generate_final_videos(
                        chapter_id,
                        chapter_params,
                        chapter["materials"],
                        audio_file,
                        chapter["subtitle_path"],
                        duration,
                        progress_callback=progress,
                    )
                    require(finals, "failed to render chapter")
                    chapter.update(
                        videos=finals, combined_videos=combined, warnings=warnings
                    )
                chapter["status"] = "complete"
                chapter["completed_stage"] = stop_at
                save()
                progress(100)
            finally:
                failure = sm.state.get_task(chapter_id)
                if failure and failure.get("state") == const.TASK_STATE_FAILED:
                    # Keep remote paid-job IDs and provider diagnostics before
                    # removing the internal chapter from task listings.
                    chapter["failure_details"] = {
                        key: value
                        for key, value in failure.items()
                        if key not in {"task_id", "state", "progress"}
                    }
                sm.state.delete_task(chapter_id)

        result["terms"] = [term for ch in chapters for term in ch["terms"]]
        if STAGES.index(stop_at) >= STAGES.index("audio"):
            duration = sum(ch["audio_duration"] for ch in chapters)
            result.update(
                audio_files=[ch["audio_file"] for ch in chapters],
                audio_duration=duration,
            )
            manifest["audio_duration"] = duration
            if (
                abs(duration - params.target_duration_minutes * 60)
                > params.target_duration_minutes * 60 * 0.15
            ):
                result["warnings"] = [
                    {
                        "code": "target_duration_mismatch",
                        "actual_duration_minutes": round(duration / 60, 2),
                    }
                ]
        if STAGES.index(stop_at) >= STAGES.index("subtitle"):
            result["subtitle_paths"] = [ch["subtitle_path"] for ch in chapters]
        if STAGES.index(stop_at) >= STAGES.index("materials"):
            result["materials"] = [item for ch in chapters for item in ch["materials"]]
        if stop_at == "video":
            active_chapter = None
            stage = "concat"
            final_path = str(directory / "final-1.mp4")
            sm.state.update_task(task_id, progress=95)
            video.concat_chapters_with_ffmpeg(
                [ch["videos"][0] for ch in chapters], final_path
            )
            result.update(
                videos=[final_path],
                combined_videos=[p for ch in chapters for p in ch["combined_videos"]],
            )
            result.setdefault("warnings", []).extend(
                dict(warning, chapter=ch["index"])
                for ch in chapters
                for warning in ch.get("warnings", [])
            )
            # Deliberately no automatic cross-posting for the long-video MVP.
        return complete(result)
    except Exception as exc:
        manifest.update(status="failed", failed_stage=stage, error=str(exc))
        if active_chapter is not None:
            active_chapter.update(status="failed", failed_stage=stage, error=str(exc))
        save()
        return tm._mark_task_failed(
            task_id,
            stage,
            str(exc),
            details={
                "failed_chapter": active_chapter["index"] if active_chapter else None,
                "chapter_failure": active_chapter.get("failure_details")
                if active_chapter
                else None,
                "chapters_manifest": str(manifest_path),
            },
        )
