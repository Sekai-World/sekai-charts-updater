import asyncio
import orjson as json
import logging
from typing import Dict, List, Tuple

import aiohttp
from anyio import Path, open_file

from crypto import unpack
from helpers import (
    ensure_dir_exists,
    get_download_list,
    refresh_cookie,
    setup_logging_queue,
)
from worker import worker

logger = logging.getLogger("charts")


async def do_download(dl_list: List[Tuple], config, headers, cookie):
    """
    Download the files in the download list using asyncio and aiohttp.
    The download list is a list of tuples containing the url and the bundle name.
    The function will use a queue to manage the download tasks.
    """
    logger.info("Starting download...")
    # Create a queue to manage tasks
    queue = asyncio.Queue()

    # Populate the queue with download tasks
    for url, bundle in dl_list:
        await queue.put((url, bundle))

    # List to track failed tasks
    failed_tasks = []

    async def worker_task(worker_id):
        nonlocal failed_tasks
        while not queue.empty():
            url, bundle = await queue.get()
            try:
                await worker(
                    f"download_worker-{worker_id}",
                    (url, bundle),
                    config,
                    headers,
                    cookie=cookie,
                )
            except Exception as e:
                # Log the error and add the task to failed_tasks
                logger.exception("Failed to download %s: %s", url, e)
                failed_tasks.append((url, bundle))
            finally:
                queue.task_done()

    # Create and run worker tasks
    workers = [
        asyncio.create_task(worker_task(worker_id))
        for worker_id in range(config.MAX_CONCURRENCY)
    ]
    await queue.join()

    # Wait for all workers to finish
    await asyncio.gather(*workers, return_exceptions=True)

    if config.ASSET_REMOTE_STORAGE:
        logger.info("Uploading music charts...")

        for remote_storage in config.ASSET_REMOTE_STORAGE:
            if remote_storage["type"] == "charts":
                remote_base = remote_storage["base"]

                # Construct the remote path
                remote_path = Path(remote_base) / config.REGION.name.lower()

                # Construct the upload command
                src_path: Path = config.ASSET_LOCAL_EXTRACTED_DIR / "charts" / config.REGION.name.lower()
                program: str = remote_storage["program"]
                args: list[str] = remote_storage["args"][:]
                args[args.index("src")] = str(src_path)
                args[args.index("dst")] = str(remote_path)
                logger.debug(
                    "Uploading %s to %s using command: %s %s",
                    src_path,
                    remote_path,
                    program,
                    " ".join(args),
                )

                # Execute the command
                upload_process = await asyncio.create_subprocess_exec(program, *args)
                await upload_process.wait()
                if upload_process.returncode != 0:
                    logger.error("Failed to upload %s to %s", src_path, remote_path)
                    raise RuntimeError(
                        f"Failed to upload {src_path} to {remote_path} using command: {program} {' '.join(args)}"
                    )
                else:
                    logger.info("Successfully uploaded %s to %s", src_path, remote_path)


async def main():
    # Check if the config module is loaded
    if "config" not in globals():
        raise ImportError(
            "Config module not loaded. Please run the script with the config argument."
        )
    # load the config module
    global config

    # ensure required directories exist
    await ensure_dir_exists(config.DL_LIST_CACHE_PATH.parent)
    await ensure_dir_exists(config.ASSET_BUNDLE_INFO_CACHE_PATH.parent)
    await ensure_dir_exists(config.GAME_VERSION_JSON_CACHE_PATH.parent)

    headers: Dict[str, str] = {
        "Accept": "*/*",
        "User-Agent": config.USER_AGENT,
        "X-Unity-Version": config.UNITY_VERSION,
    }

    cookie = None
    # Cookie must be filled if GAME_COOKIE_URL is set in the config
    if config.GAME_COOKIE_URL:
        headers, cookie = await refresh_cookie(config, headers)

    if await config.DL_LIST_CACHE_PATH.exists():
        logger.info(
            "Cache file %s exists, loading from cache", config.DL_LIST_CACHE_PATH
        )
        # Load the dl_list from the cache and start downloading
        async with await open_file(config.DL_LIST_CACHE_PATH, "r") as f:
            dl_list = json.loads(await f.read())
            logger.info("%d items to download", len(dl_list))
            await do_download(dl_list, config=config, headers=headers, cookie=cookie)

        # remove the cache file
        await config.DL_LIST_CACHE_PATH.unlink()
        return

    game_version_json = None
    # Download, parse and cache the game version json from GAME_VERSION_JSON_URL
    if config.GAME_VERSION_JSON_URL:
        async with aiohttp.ClientSession() as session:
            async with session.get(config.GAME_VERSION_JSON_URL) as response:
                if response.status == 200:
                    game_version_json = await response.json(content_type="text/plain")
                    # Check if the json is valid
                    if (
                        not isinstance(game_version_json, dict)
                        or "appVersion" not in game_version_json
                        or "appHash" not in game_version_json
                    ):
                        raise Exception(
                            f"Invalid json from {config.GAME_VERSION_JSON_URL}"
                        )
                else:
                    raise Exception(
                        f"Failed to fetch game version json from {config.GAME_VERSION_JSON_URL}"
                    )
    else:
        raise Exception("GAME_VERSION_JSON_URL is not set in the config")
    logger.debug(
        "Current appVersion: %s, dataVersion: %s, assetVersion: %s",
        game_version_json["appVersion"],
        game_version_json["dataVersion"],
        game_version_json["assetVersion"],
    )

    assetbundle_host_hash = None
    # Format GAME_VERSION_URL using the appVersion and appHash from the game version json
    if config.GAME_VERSION_URL:
        game_version_url = config.GAME_VERSION_URL.format(
            appVersion=game_version_json["appVersion"],
            appHash=game_version_json["appHash"],
        )
        # This request needs to be proxied
        async with aiohttp.ClientSession(proxy=config.PROXY_URL) as session:
            async with session.get(game_version_url, headers=headers) as response:
                if response.status == 200:
                    result = await response.read()
                    json_result = unpack(config.AES_KEY, config.AES_IV, result)
                    # Check if the json is valid
                    if (
                        not isinstance(json_result, dict)
                        or "assetbundleHostHash" not in json_result
                    ):
                        raise Exception(f"Invalid result from {game_version_url}")
                    assetbundle_host_hash = json_result["assetbundleHostHash"]
                else:
                    raise Exception(
                        f"Failed to fetch assetbundle host hash from {game_version_url}"
                    )
    else:
        raise Exception("GAME_VERSION_URL is not set in the config")
    logger.debug(
        "Current assetbundleHostHash: %s, assetHash: %s",
        assetbundle_host_hash,
        game_version_json["assetHash"],
    )

    asset_bundle_info = None
    # Format ASSET_BUNDLE_INFO_URL using the information above
    if config.ASSET_BUNDLE_INFO_URL:
        asset_bundle_info_url = config.ASSET_BUNDLE_INFO_URL.format(
            assetbundleHostHash=assetbundle_host_hash,
            assetVersion=game_version_json["assetVersion"],
            assetHash=game_version_json["assetHash"],
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(asset_bundle_info_url, headers=headers) as response:
                if response.status == 200:
                    result = await response.read()
                    asset_bundle_info = unpack(config.AES_KEY, config.AES_IV, result)
                    # Check if the json is valid
                    if not isinstance(asset_bundle_info, dict):
                        raise Exception(f"Invalid json from {asset_bundle_info_url}")
                else:
                    raise Exception(
                        f"Failed to fetch asset bundle info from {asset_bundle_info_url}"
                    )
    else:
        raise Exception("ASSET_BUNDLE_INFO_URL is not set in the config")
    logger.debug(
        "Current assetBundleInfoVersion: %s, bundles length: %d",
        asset_bundle_info["version"],
        len(asset_bundle_info["bundles"]),
    )

    # Generate the download list
    download_list = await get_download_list(
        asset_bundle_info,
        game_version_json,
        config=config,
        assetbundle_host_hash=assetbundle_host_hash,
    )
    logger.info("Download list generated, %d items to download", len(download_list))

    await do_download(download_list, config=config, headers=headers, cookie=cookie)

    # remove the cached download list
    if await config.DL_LIST_CACHE_PATH.exists():
        await config.DL_LIST_CACHE_PATH.unlink()


def cli():
    # Accept command line arguments
    import argparse

    parser = argparse.ArgumentParser(
        description="Start the asset updater with given config."
    )
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        help="Path to the config python file.",
        required=True,
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging."
    )
    args = parser.parse_args()

    # Load the config python file as dynamic module
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("config", args.config)
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    sys.modules["config"] = config
    # Set the config as a global variable
    globals()["config"] = config

    # Set the logging level
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    setup_logging_queue()

    # Run the main function
    asyncio.run(main())


if __name__ == "__main__":
    cli()
