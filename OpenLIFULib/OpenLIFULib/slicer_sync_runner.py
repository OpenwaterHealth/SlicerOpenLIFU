import time
import threading
import logging
from openlifu.cloud.cloud import Cloud
from OpenLIFUCloudSync import getCloudSyncLogic


class SlicerSyncRunner:
    def __init__(self, db_path, api_token, on_access_token_refresh=None):
        self.db_path = db_path
        self.api_token = api_token
        self.running = False
        self._thread = None
        self._on_access_token_refresh = on_access_token_refresh

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_process)
        self._thread.daemon = True
        self._thread.start()

    def _run_process(self):
        cloud = Cloud()

        last_refresh_time = time.time()
        refresh_interval = 3000

        try:
            cloud.set_access_token(self.api_token)

            cloud.start(self.db_path)
            cloud.sync()
            cloud.start_background_sync()

            while self.running:
                time.sleep(0.01)

                if time.time() - last_refresh_time > refresh_interval:
                    try:
                        new_token = getCloudSyncLogic().getValidToken()
                        cloud.set_access_token(new_token)
                        last_refresh_time = time.time()
                        logging.info(
                            "Token refreshed successfully in background.")
                    except Exception as e:
                        logging.error(f"Background token refresh failed: {e}")

        except Exception as e:
            logging.error(f"Background Sync Error: {e}")
        finally:
            cloud.stop()

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join()
