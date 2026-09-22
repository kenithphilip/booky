"""Minimal qBittorrent Web API client for the P2P download path."""
import requests, config

class Qbit:
    def __init__(self):
        self.s = requests.Session()
        self.base = config.QBIT_URL.rstrip("/")
        if config.QBIT_USER:
            self.s.post(f"{self.base}/api/v2/auth/login",
                        data={"username": config.QBIT_USER, "password": config.QBIT_PASS}, timeout=15)

    def add(self, url_or_magnet, save_path):
        data = {"category": config.QBIT_CATEGORY, "savepath": save_path, "autoTMM": "false"}
        if url_or_magnet.startswith("magnet:"):
            data["urls"] = url_or_magnet
            files = None
        else:
            data["urls"] = url_or_magnet     # qB can fetch a .torrent by URL
            files = None
        r = self.s.post(f"{self.base}/api/v2/torrents/add", data=data, files=files, timeout=30)
        r.raise_for_status()
