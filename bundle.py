"""This module contains functions to download, deobfuscate, and extract asset bundles."""

from asyncio import Lock
import logging
from typing import Dict, List, Tuple

import aiohttp
import orjson as json
import UnityPy
import UnityPy.classes
import UnityPy.config
from anyio import Path, open_file

from constants import UNITY_FS_CONTAINER_BASE
from helpers import deobfuscate
from utils.chart import get_list, get_json_url, render_chart

logger = logging.getLogger("live2d")


async def download_deobfuscate_bundle(
    url: str, bundle_save_path: Path, headers: Dict[str, str]
) -> Tuple[str, Dict]:
    """Download and deobfuscate the bundle."""
    # Download the bundle
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                # Read the response data
                data = await response.read()
                # Deobfuscate the data
                deobfuscated_data = await deobfuscate(data)
                # Save the deobfuscated data to the file
                async with await open_file(bundle_save_path, "wb") as f:
                    await f.write(deobfuscated_data)
            else:
                raise aiohttp.ClientError(f"Failed to download {url}")


async def extract_asset_bundle(
    bundle_save_path: Path,
    bundle: Dict[str, str],
    extracted_save_path: Path,
    unity_version: str = None,
    config=None,
) -> List[Path]:
    """Extract the asset bundle to the specified directory.

    Args:
        bundle_save_path (Path): _description_
        bundle (Dict[str, str]): _description_
        extracted_save_path (Path): _description_
        unity_version (str, optional): _description_. Defaults to None.
        config (_type_, optional): _description_. Defaults to None.

    Raises:
        ValueError: _description_
        TypeError: _description_
        TypeError: _description_
        TypeError: _description_
        RuntimeError: _description_

    Returns:
        List[Path]: _description_
    """
    UnityPy.config.FALLBACK_UNITY_VERSION = unity_version

    # Load the bundle
    _unity_file = UnityPy.load(bundle_save_path.as_posix())
    # Check if the bundle is valid
    if not _unity_file:
        raise ValueError(f"Failed to load {bundle_save_path}")

    logger.debug("Loaded bundle %s from %s", bundle.get("bundleName"), bundle_save_path)

    exported_files: List[Path] = []
    score_files: List[Path] = []

    # Extract the bundle
    for unityfs_path, unityfs_obj in _unity_file.container.items():
        relpath = Path(unityfs_path).relative_to(UNITY_FS_CONTAINER_BASE)

        save_path = extracted_save_path / relpath.relative_to(*relpath.parts[:1])

        save_dir = save_path.parent
        if "motion" in save_dir.parts[-1]:
            # Skip the motions for now
            logger.debug("Skipping motion %s", unityfs_path)
            continue
        # Create the directory if it doesn't exist
        await save_dir.mkdir(parents=True, exist_ok=True)

        try:
            match unityfs_obj.type.name:
                case "MonoBehaviour":
                    tree = None
                    try:
                        if unityfs_obj.serialized_type.node:
                            tree = unityfs_obj.read_typetree()
                    except AttributeError:
                        tree = unityfs_obj.read_typetree()
                    logger.debug(
                        "Saving MonoBehaviour %s to %s", unityfs_path, save_path
                    )
                    # Save the typetree to a json file
                    async with await open_file(save_path, "wb") as f:
                        await f.write(json.dumps(tree, option=json.OPT_INDENT_2))
                    exported_files.append(save_path)
                case "TextAsset":
                    data = unityfs_obj.read()
                    if isinstance(data, UnityPy.classes.TextAsset):
                        if save_path.suffix == ".bytes":
                            save_path = save_path.with_suffix("")
                        async with await open_file(save_path, "wb") as f:
                            data_bytes = data.m_Script.encode(
                                "utf-8", "surrogateescape"
                            )
                            await f.write(data_bytes)
                        if (
                            "music_score" in save_path.parts
                            and save_path.suffix == ".txt"
                        ):
                            # Save the score files to a separate list
                            score_files.append(save_path)
                        exported_files.append(save_path)
                    else:
                        raise TypeError(
                            f"Expected TextAsset, got {type(data)} for {unityfs_path}"
                        )
                case _:
                    logger.warning(
                        "Unknowen type %s of %s, extracting typetree",
                        unityfs_obj.type.name,
                        unityfs_path,
                    )
                    tree = unityfs_obj.read_typetree()
                    async with await open_file(save_path, "wb") as f:
                        await f.write(json.dumps(tree, option=json.OPT_INDENT_2))
                    exported_files.append(save_path)
        except (ValueError, TypeError, AttributeError, OSError) as e:
            logger.error("Failed to extract %s: %s", unityfs_path, e)
            continue

        # Post processing for score files
        if score_files:
            # Download musio info json from remote
            music_info_url = get_json_url(config.REGION.name.lower(), "musics")
            music_info = await get_list(music_info_url)

            for score_file in score_files:
                padded_music_id = score_file.parent.stem.split("_")[0]
                music_id = int(padded_music_id)
                padded_music_id_3 = str(music_id).zfill(3)

                # Find the music for the score file
                music = next((m for m in music_info if m["id"] == music_id), None)
                if music is None:
                    logger.exception(
                        "Failed to find music for score file %s", score_file
                    )
                    raise RuntimeError(
                        f"Failed to find music for score file {score_file}"
                    )

                # Create the chart path
                chart_path: Path = (
                    config.ASSET_LOCAL_EXTRACTED_DIR
                    / "charts"
                    / config.REGION.name.lower()
                    / padded_music_id
                    / score_file.with_suffix(".svg").name
                )
                await chart_path.parent.mkdir(parents=True, exist_ok=True)

                # Render the chart
                await render_chart(
                    score_file.as_posix(),
                    chart_path.as_posix(),
                    music,
                    f"https://storage.sekai.best/sekai-{config.REGION.name.lower()}-assets/music/jacket/jacket_s_{padded_music_id_3}/jacket_s_{padded_music_id_3}.png",
                )
                logger.info("Rendered chart for %s to %s", score_file, chart_path)

    return exported_files
