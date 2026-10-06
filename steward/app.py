"""Composition root: wires config + pack + store + gate + tools + runtime + queue."""
from __future__ import annotations

import os
from types import SimpleNamespace

from .audit import AuditLog, EvidenceStore
from .config import Config
from .gate import Gate
from .llm import make_llm
from .memory import CompanyPack, Memory
from .policy import PolicyEngine
from .queue import TaskQueue
from .runtime import Runtime
from .store import Store
from .tools.base import ToolRegistry
from .tools.browser import BrowserSession, register_browser_tools
from .tools.standard import register_standard_tools
from .worker import Worker


def build(cfg: Config | None = None, llm=None):
    cfg = cfg or Config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    audit = AuditLog(store)
    evidence = EvidenceStore(store, audit, cfg.evidence_dir)
    pack = CompanyPack(cfg.pack_dir)
    engine = PolicyEngine(pack.policies)
    gate = Gate(store, audit, engine)
    memory = Memory(store, pack)
    queue = TaskQueue(store, audit)
    registry = ToolRegistry()
    register_browser_tools(registry)
    register_standard_tools(registry, cfg=cfg, pack=pack)

    def env_factory(task_id, spec):
        return {"browser": BrowserSession(cfg=cfg, gate=gate, audit=audit, evidence=evidence, systems=pack.systems,
                                          task_id=task_id, role=cfg.role, task_ctx={"id": task_id})}

    def get_llm():
        nonlocal llm
        if llm is None:
            llm = make_llm()
        return llm

    def runtime_factory() -> Runtime:
        llm = get_llm()
        return Runtime(cfg=cfg, store=store, audit=audit, evidence=evidence, gate=gate, memory=memory, registry=registry,
                       llm=llm, env_factory=env_factory, company=pack.directory.get("company", "Northwind Retail"))

    return SimpleNamespace(cfg=cfg, store=store, audit=audit, evidence=evidence, pack=pack, gate=gate, memory=memory,
                           queue=queue, registry=registry, runtime_factory=runtime_factory, get_llm=get_llm,
                           worker=lambda wid=None: Worker(queue, runtime_factory, wid))
