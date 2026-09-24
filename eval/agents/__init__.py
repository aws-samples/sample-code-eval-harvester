"""Concrete Harbor agents this harness ships as starters.

Modules here subclass a Harbor ``BaseAgent`` and are loaded by import path via ``harbor run -a
module:Class`` (Harbor's ``AgentFactory`` treats any ``-a`` value containing ``:`` as a custom
import path — no Harbor fork needed). They import ``harbor``, so they live behind the ``eval``
dependency group and are never imported by :mod:`eval.agent` (which must stay harbor-free so the
offline suite can name the agents without the group synced).
"""
