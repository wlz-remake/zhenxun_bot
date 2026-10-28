import asyncio
from pathlib import Path

from zhenxun.services.log import logger

from ..config import PLUGIN_TEMP_DIR


async def merge_media_files(
    video_path: Path,
    audio_path: Path | None,
    output_path: Path,
    log_output: bool = False,
) -> bool:
    """
    合并音视频文件，并确保 FFmpeg 进程在任何情况下都能被正确终止。
    - 优先尝试直接复制所有流。
    - 如果失败，则回退到仅重新编码音频流的模式，以解决兼容性问题并保持性能。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not video_path.exists():
        logger.error(f"视频文件不存在: {video_path}")
        return False

    if audio_path and not audio_path.exists():
        logger.error(f"音频文件不存在: {audio_path}")
        return False

    async def _run_ffmpeg(vcodec: str, acodec: str) -> tuple[bool, str]:
        """运行FFmpeg命令并确保其进程被管理"""
        command = ["ffmpeg", "-y", "-i", str(video_path.resolve())]
        if audio_path:
            command.extend(["-i", str(audio_path.resolve())])

        command.extend(["-c:v", vcodec])
        if audio_path:
            command.extend(["-c:a", acodec])

        command.extend(["-nostdin", "-loglevel", "error", str(output_path.resolve())])

        logger.info(f"执行FFmpeg命令: {' '.join(command)}")

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE
                if log_output
                else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()

            error_msg = stderr.decode("utf-8", errors="ignore") if stderr else ""

            if process.returncode != 0:
                logger.error(
                    f"FFmpeg执行失败 (vcodec={vcodec}, acodec={acodec}): {error_msg}"
                )
                return False, error_msg

            if not output_path.exists() or output_path.stat().st_size == 0:
                logger.error(f"FFmpeg执行后文件不存在或为空: {output_path}")
                return False, "输出文件为空"

            return True, ""

        except FileNotFoundError:
            # FFmpeg 未安装或不在 PATH 中时，create_subprocess_exec 会抛出 FileNotFoundError。
            # 此处捕获并返回失败，避免异常向上层传播导致原始的
            # "[Errno 2] No such file or directory: 'ffmpeg'" 错误信息直接暴露给用户。
            logger.error("FFmpeg 未找到，请确保已安装 FFmpeg 并添加到 PATH 环境变量")
            return False, "FFmpeg未找到"

        finally:
            if process and process.returncode is None:
                logger.warning(f"FFmpeg 进程 ({process.pid}) 未正常结束，将强制终止。")
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        f"终止 FFmpeg 进程 ({process.pid}) 超时，将强制杀死。"
                    )
                    process.kill()
                except Exception as e:
                    logger.error(f"终止/杀死 FFmpeg 进程时出错: {e}")

    logger.info("尝试快速合并 (stream copy)...")
    success, error_log = await _run_ffmpeg("copy", "copy" if audio_path else "")

    if not success:
        logger.warning(
            f"快速合并失败，回退到音频重编码模式。失败原因: {error_log[:200]}..."
        )
        success, error_log = await _run_ffmpeg("copy", "aac" if audio_path else "")

    if success:
        logger.info(f"媒体文件处理成功: {output_path.name}")
    else:
        logger.error(f"所有合并策略均失败。最终错误: {error_log[:200]}")

    return success


def get_temp_file_path(
    prefix: str,
    suffix: str,
    identifier: str,
    title: str | None = None,
) -> Path:
    """生成临时文件路径"""
    filename = f"{prefix}{identifier}{suffix}"
    return PLUGIN_TEMP_DIR / filename


async def merge_durl_segments(segment_paths: list[Path], output_path: Path) -> bool:
    """合并多个 DURL 分段为一个完整文件

    使用 FFmpeg 的 concat demuxer 进行无损拼接，适用于 FLV/MP4 分段。
    临时文件列表在合并完成后自动清理。

    Args:
        segment_paths: 分段文件路径列表（按顺序）
        output_path: 合并后的输出文件路径

    Returns:
        合并是否成功
    """
    if not segment_paths:
        logger.error("DURL 分段列表为空，无法合并")
        return False

    if len(segment_paths) == 1:
        # 单分段无需合并，直接复制
        import shutil

        output_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, segment_paths[0], output_path)
        return True

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 检查所有分段文件是否存在
    for seg_path in segment_paths:
        if not seg_path.exists():
            logger.error(f"DURL 分段文件不存在: {seg_path}")
            return False

    # 创建 concat demuxer 所需的文件列表
    list_file = PLUGIN_TEMP_DIR / f"{output_path.stem}_concat_list.txt"
    try:
        # 使用绝对路径并替换反斜杠为正斜杠，确保 FFmpeg concat demuxer 兼容
        list_content = "\n".join(
            f"file '{str(seg_path.resolve()).replace(chr(92), '/')}'"
            for seg_path in segment_paths
        )
        list_file.write_text(list_content, encoding="utf-8")
        logger.debug(f"已生成 concat 文件列表: {list_file}")

        command = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file.resolve()),
            "-c",
            "copy",
            "-nostdin",
            "-loglevel",
            "error",
            str(output_path.resolve()),
        ]

        logger.info(f"执行 FFmpeg concat 命令合并 {len(segment_paths)} 个分段")

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()

            error_msg = stderr.decode("utf-8", errors="ignore") if stderr else ""

            if process.returncode != 0:
                logger.error(f"FFmpeg concat 合并失败: {error_msg}")
                return False

            if not output_path.exists() or output_path.stat().st_size == 0:
                logger.error(f"FFmpeg concat 合并后文件不存在或为空: {output_path}")
                return False

            logger.info(
                f"DURL 分段合并成功: {len(segment_paths)} 个分段 -> {output_path.name}"
            )
            return True

        finally:
            if process and process.returncode is None:
                logger.warning(
                    f"FFmpeg concat 进程 ({process.pid}) 未正常结束，将强制终止。"
                )
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    process.kill()
                except Exception as e:
                    logger.error(f"终止 FFmpeg concat 进程时出错: {e}")

    except Exception as e:
        logger.error(f"合并 DURL 分段时发生异常: {e}", e=e)
        return False
    finally:
        # 清理 concat 文件列表
        try:
            if list_file.exists():
                list_file.unlink()
        except Exception as e:
            logger.warning(f"清理 concat 文件列表失败: {e}")


async def check_ffmpeg_available() -> bool:
    """检查FFmpeg是否可用"""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.communicate()
        is_available = process.returncode == 0

        if is_available:
            logger.debug("FFmpeg可用", "B站解析")
        else:
            logger.warning("FFmpeg不可用，返回码非零", "B站解析")

        return is_available
    except FileNotFoundError:
        logger.warning(
            "FFmpeg未找到，请确保已安装FFmpeg并添加到PATH环境变量", "B站解析"
        )
        return False
    except Exception as e:
        logger.debug(f"检查FFmpeg可用性失败: {e}", e=e)
        return False
