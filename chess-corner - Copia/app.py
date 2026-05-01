from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

import atexit
import logging
import os
import platform
import queue
import subprocess
import threading
import time
from collections import OrderedDict

import chess


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

THREADS = int(os.getenv("STOCKFISH_THREADS", "2"))
HASH_SIZE = int(os.getenv("STOCKFISH_HASH", "128"))
FAST_DEPTH = int(os.getenv("FAST_DEPTH", "16"))
DEEP_DEPTH = int(os.getenv("DEEP_DEPTH", "20"))
MOVETIME_MS = int(os.getenv("STOCKFISH_MOVETIME_MS", "2500"))
ANALYSIS_TIMEOUT = float(os.getenv("ANALYSIS_TIMEOUT", "10"))
CACHE_SIZE = int(os.getenv("CACHE_SIZE", "500"))


def get_stockfish_path():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.getenv("STOCKFISH_PATH")

    candidates = []
    if env_path:
        candidates.append(env_path if os.path.isabs(env_path) else os.path.join(base_dir, env_path))

    # Mantieni il binario ARMv8 come scelta primaria del progetto.
    candidates.extend(
        [
            os.path.join(base_dir, "stockfish", "stockfish-android-armv8"),
            os.path.join(base_dir, "stockfish", "stockfish-windows-x64.exe"),
            os.path.join(base_dir, "stockfish", "stockfish-macos"),
            os.path.join(base_dir, "stockfish", "stockfish"),
        ]
    )

    current_system = platform.system()
    if current_system == "Windows":
        candidates.insert(1, os.path.join(base_dir, "stockfish", "stockfish-windows-x64.exe"))
    elif current_system == "Darwin":
        candidates.insert(1, os.path.join(base_dir, "stockfish", "stockfish-macos"))
    elif current_system == "Linux":
        candidates.insert(1, os.path.join(base_dir, "stockfish", "stockfish"))

    seen = set()
    for path in candidates:
        resolved = os.path.abspath(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        if os.path.isfile(resolved):
            return resolved

    raise FileNotFoundError(
        "Stockfish non trovato. Posiziona il binario in stockfish/stockfish-android-armv8 "
        "o imposta STOCKFISH_PATH con il percorso corretto."
    )


class LRUCache:
    def __init__(self, max_size=CACHE_SIZE):
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.max_size = max_size

    def get(self, key):
        with self.lock:
            if key not in self.cache:
                return None
            self.cache.move_to_end(key)
            return self.cache[key]

    def set(self, key, value):
        with self.lock:
            self.cache[key] = value
            self.cache.move_to_end(key)
            if len(self.cache) > self.max_size:
                self.cache.popitem(last=False)


class StockfishEngine:
    def __init__(self, path):
        self.path = path
        self.process = None
        self.lock = threading.Lock()
        self.output = queue.Queue()
        self.reader_thread = None
        self.initialized = False

    def start(self):
        if self.initialized and self.process and self.process.poll() is None:
            return

        if not os.path.isfile(self.path):
            raise FileNotFoundError(f"Stockfish non trovato: {self.path}")

        if platform.system() != "Windows":
            try:
                os.chmod(self.path, os.stat(self.path).st_mode | 0o111)
            except OSError as exc:
                logger.warning("Impossibile rendere eseguibile Stockfish: %s", exc)

        self.process = subprocess.Popen(
            [self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.output = queue.Queue()
        self.reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader_thread.start()

        self._send("uci")
        self._read_until("uciok", timeout=10)
        self._send(f"setoption name Threads value {THREADS}")
        self._send(f"setoption name Hash value {HASH_SIZE}")
        self._send("setoption name Ponder value false")
        self._send("setoption name MultiPV value 1")
        self._send("isready")
        self._read_until("readyok", timeout=10)

        self.initialized = True
        logger.info("Stockfish inizializzato: %s", self.path)

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                self.output.put(line.strip())
        except Exception as exc:
            self.output.put(exc)

    def _send(self, command):
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("Processo Stockfish non attivo")
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def _read_line(self, timeout):
        item = self.output.get(timeout=timeout)
        if isinstance(item, Exception):
            raise item
        return item

    def _read_until(self, token, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._read_line(max(0.1, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise TimeoutError(f"Timeout in attesa di {token}") from exc
            if token in line:
                return line
        raise TimeoutError(f"Timeout in attesa di {token}")

    def analyze(self, fen, depth, movetime_ms=MOVETIME_MS):
        with self.lock:
            self.start()
            self._drain_output()
            self._send(f"position fen {fen}")
            self._send(f"go depth {depth} movetime {movetime_ms}")

            bestmove = None
            evaluation = 0.0
            last_depth = 0
            pv = ""
            deadline = time.monotonic() + ANALYSIS_TIMEOUT

            while time.monotonic() < deadline:
                try:
                    line = self._read_line(max(0.1, deadline - time.monotonic()))
                except queue.Empty as exc:
                    self._send("stop")
                    raise TimeoutError("Timeout durante l'analisi") from exc

                if line.startswith("info") and "score" in line:
                    parsed = parse_info_line(line)
                    if parsed["depth"] >= last_depth:
                        last_depth = parsed["depth"]
                        evaluation = parsed["evaluation"]
                        pv = parsed["pv"]

                if line.startswith("bestmove"):
                    parts = line.split()
                    if len(parts) > 1 and parts[1] != "(none)":
                        bestmove = parts[1]
                    break
            else:
                self._send("stop")
                raise TimeoutError("Timeout durante l'analisi")

            return {
                "bestmove": bestmove,
                "evaluation": evaluation,
                "depth": last_depth,
                "pv": pv,
            }

    def _drain_output(self):
        while True:
            try:
                self.output.get_nowait()
            except queue.Empty:
                return

    def close(self):
        if not self.process:
            return

        try:
            if self.process.poll() is None:
                self._send("quit")
                self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            logger.warning("Stockfish forzato alla chiusura")
        except Exception as exc:
            logger.error("Errore chiusura Stockfish: %s", exc)
        finally:
            self.initialized = False
            self.process = None


def parse_info_line(line):
    parts = line.split()
    depth = 0
    evaluation = 0.0
    pv = ""

    try:
        if "depth" in parts:
            depth = int(parts[parts.index("depth") + 1])
        if "cp" in parts:
            evaluation = int(parts[parts.index("cp") + 1]) / 100.0
        elif "mate" in parts:
            evaluation = f"mate {parts[parts.index('mate') + 1]}"
        if "pv" in parts:
            pv = " ".join(parts[parts.index("pv") + 1 :])
    except (ValueError, IndexError):
        logger.debug("Riga Stockfish non parsabile: %s", line)

    return {"depth": depth, "evaluation": evaluation, "pv": pv}


def validate_fen(fen):
    try:
        chess.Board(fen)
        return True
    except ValueError:
        return False


engine = None
engine_lock = threading.Lock()
cache = LRUCache()


def get_engine():
    global engine
    with engine_lock:
        if engine is None:
            engine = StockfishEngine(get_stockfish_path())
        return engine


def cleanup():
    global engine
    with engine_lock:
        if engine:
            engine.close()
            engine = None


atexit.register(cleanup)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    try:
        stockfish_path = get_stockfish_path()
        stockfish_available = True
    except FileNotFoundError:
        stockfish_path = None
        stockfish_available = False

    return jsonify(
        {
            "ok": True,
            "stockfish_available": stockfish_available,
            "stockfish_path": stockfish_path,
        }
    )


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    fen = data.get("fen", "").strip()
    mode = data.get("mode", "fast")

    if not fen:
        return jsonify({"error": "FEN mancante"}), 400
    if not validate_fen(fen):
        return jsonify({"error": "FEN non valida"}), 400

    depth = DEEP_DEPTH if mode == "deep" else FAST_DEPTH
    cache_key = f"{fen}|{depth}|{MOVETIME_MS}"
    cached = cache.get(cache_key)
    if cached:
        return jsonify(cached)

    try:
        result = get_engine().analyze(fen, depth)
        cache.set(cache_key, result)
        return jsonify(result)
    except FileNotFoundError as exc:
        logger.error("Stockfish non trovato: %s", exc)
        return jsonify({"error": str(exc)}), 500
    except TimeoutError as exc:
        logger.error("Timeout Stockfish: %s", exc)
        return jsonify({"error": str(exc)}), 504
    except Exception:
        logger.exception("Errore imprevisto durante l'analisi")
        return jsonify({"error": "Errore interno del server"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
