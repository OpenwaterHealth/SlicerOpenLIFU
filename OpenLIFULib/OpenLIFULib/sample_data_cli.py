# Standard library imports
import argparse
import importlib.util
import json
import sys
from pathlib import Path


def _load_sample_data_module():
    sample_data_path = Path(__file__).resolve().with_name("sample_data.py")
    spec = importlib.util.spec_from_file_location("openlifu_sample_data", sample_data_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sample_data = _load_sample_data_module()


def _emit_progress_event(event: dict) -> None:
    print(sample_data.PROGRESS_LINE_PREFIX + json.dumps(event), flush=True)


def _progress_callback(message: str, value: int, maximum: int) -> None:
    _emit_progress_event(
        {
            "message": message,
            "value": value,
            "maximum": maximum,
        }
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create an OpenLIFU sample database.")
    parser.add_argument("--destination", required=True, help="Empty folder where the database will be installed.")
    parser.add_argument("--work-dir", required=True, help="Empty temporary work directory owned by the parent process.")
    parser.add_argument(
        "--archive-url",
        default=sample_data.SAMPLE_DATABASE_ARCHIVE_URL,
        help="Sample database archive URL. Defaults to the pinned OpenLIFU sample database release.",
    )
    parser.add_argument(
        "--cancel-file",
        default="",
        help="Optional sentinel file. If it exists, setup exits as canceled.",
    )
    args = parser.parse_args(argv)
    cancel_file = Path(args.cancel_file) if args.cancel_file else None

    try:
        sample_data.copy_sample_database_from_archive(
            Path(args.destination),
            progress_callback=_progress_callback,
            archive_url=args.archive_url,
            work_dir=Path(args.work_dir),
            cancel_callback=cancel_file.exists if cancel_file is not None else None,
        )
    except sample_data.SampleDatabaseSetupCanceled as exc:
        _emit_progress_event(
            {
                "message": str(exc),
                "value": 0,
                "maximum": 0,
                "success": False,
                "canceled": True,
            }
        )
        return 2
    except Exception as exc:
        _emit_progress_event(
            {
                "message": str(exc),
                "value": 0,
                "maximum": 0,
                "success": False,
                "error": str(exc),
            }
        )
        print(str(exc), file=sys.stderr, flush=True)
        return 1

    _emit_progress_event(
        {
            "message": "Sample database created.",
            "value": 1,
            "maximum": 1,
            "success": True,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
