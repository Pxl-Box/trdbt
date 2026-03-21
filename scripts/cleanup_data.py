from pathlib import Path
import json
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def main():
    try:
        config_path = Path("node_config.json")
        if config_path.exists():
            with open(config_path) as f:
                shared_path = json.load(f).get("shared_drive_path", r"D:\trd-data")
        else:
            shared_path = r"D:\trd-data"
            
        d = Path(shared_path) / "processed_data"
        
        if not d.exists():
            logging.info(f"Directory {d} does not exist. Nothing to clear.")
            return

        deleted = 0
        for f in d.glob("*.parquet"):
            try:
                f.unlink()
                deleted += 1
            except Exception as e:
                logging.warning(f"Could not delete {f.name}: {e}")
        
        logging.info(f"Successfully cleared {deleted} stale processed files from {d}")
    except Exception as e:
        logging.error(f"Cleanup failed: {e}")

if __name__ == "__main__":
    main()
