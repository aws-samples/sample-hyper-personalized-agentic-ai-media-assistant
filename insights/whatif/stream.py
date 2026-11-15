"""Live event stream for a what-if run.

The worker appends events to the insights runs table as they happen; the
console polls them (API Gateway cannot stream from a Python Lambda). Model
output is coalesced into one delta per stage per flush (~0.3 s), so a stream
costs a few dozen writes, not one per token.

`streaming_call` is the agent's llm.call with a token callback attached: the
same model, prompt, temperature and token limit, so the output is the one the
live agent would get. Only the callback differs, and only to observe.
"""

import json
import threading
import time

import boto3

import llm
from strands import Agent

_local = threading.local()


class Emitter:
    def __init__(self, table_name, run_id, ttl_s=24 * 3600):
        self.table = boto3.resource("dynamodb").Table(table_name)
        self.run_id = run_id
        self.ttl = int(time.time() + ttl_s)
        self.started = time.time()
        self.seq = 0
        self.lock = threading.Lock()
        self.pending = []
        self.deltas = {}            # (side, stage) -> text not yet flushed
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def emit(self, etype, side=None, **data):
        with self.lock:
            self._flush_deltas_locked()
            self.pending.append((etype, side, data))

    def delta(self, side, stage, text):
        with self.lock:
            self.deltas[(side, stage)] = self.deltas.get((side, stage), "") + text

    def _flush_deltas_locked(self):
        for (side, stage), text in self.deltas.items():
            if text:
                self.pending.append(("llm_delta", side, {"stage": stage, "text": text}))
        self.deltas = {}

    def _write(self):
        with self.lock:
            self._flush_deltas_locked()
            batch, self.pending = self.pending, []
        if not batch:
            return
        with self.table.batch_writer() as w:
            for kind, side, data in batch:
                self.seq += 1
                w.put_item(Item={"run_id": self.run_id, "seq": self.seq, "type": kind, "side": side or "",
                                 "t": str(round(time.time() - self.started, 2)),
                                 "data": json.dumps(data, default=str), "expires_at": self.ttl})

    def _loop(self):
        while not self.stop.wait(0.3):
            try:
                self._write()
            except Exception as exc:  # noqa: BLE001 - a lost progress write must not fail the run
                print(f"[whatif] stream write failed: {exc!r}")

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self._write()


def bind(emitter, side):
    """Route this thread's events (the agent runs its LLM stages on the calling thread)."""
    _local.emitter, _local.side = emitter, side


def current():
    return getattr(_local, "emitter", None), getattr(_local, "side", None)


def emit(etype, **data):
    em, side = current()
    if em is not None:
        em.emit(etype, side, **data)


def _system_text(system):
    if isinstance(system, str):
        return system
    return "".join(b.get("text", "") for b in system or [] if isinstance(b, dict))


def streaming_call(stage):
    def call(system, user_input, model_id, temperature=llm.V2_TEMPERATURE, max_tokens=llm.V2_MAX_TOKENS):
        em, side = current()
        if em is None:
            return llm.call(system, user_input, model_id, temperature, max_tokens)
        model = llm._get_model(model_id, temperature, max_tokens)

        def on_event(**kw):
            if kw.get("data"):
                em.delta(side, stage, kw["data"])

        sys_text = _system_text(system)
        em.emit("llm_start", side, stage=stage, model=model_id,
                # Haiku 5.x takes no temperature (llm._get_model drops it).
                temperature=None if llm.is_haiku_5(model_id) else temperature, max_tokens=max_tokens,
                system_chars=len(sys_text), system_head=sys_text[:1500], input=user_input[:12000])
        agent = Agent(model=model, system_prompt=system, callback_handler=on_event)
        t0 = time.perf_counter()
        result = agent(user_input)
        elapsed = time.perf_counter() - t0
        text = str(result).strip()
        usage = llm.extract_usage(result)
        em.emit("llm_end", side, stage=stage, text=text[:12000], usage=usage, seconds=round(elapsed, 2))
        return text, usage, elapsed
    return call


def install():
    """Point the agent's understand and rerank stages at the observing call."""
    import answer
    import reduce
    import understand
    understand.call = streaming_call("understand")
    reduce.call = streaming_call("rerank")
    answer.call = streaming_call("answer")
