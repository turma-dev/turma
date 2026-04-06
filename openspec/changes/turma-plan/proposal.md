# Proposal: turma plan (single-pass author)

## Summary

Implement `turma plan --feature <name>` as a single-pass author mode. This is
the first command that invokes an external AI agent, loads runtime config, and
produces OpenSpec artifacts.

## Motivation

The planning phase is Turma's core differentiator. Before any swarm execution
can happen, a feature needs a reviewed spec. Today `turma plan` is a stub.
Implementing the single-pass author version validates config loading, agent
subprocess invocation, and OpenSpec integration — the three foundational
capabilities every subsequent feature depends on.

## User Story

A developer runs `turma plan --feature oauth-auth` in a repo that has
`turma.toml` and `.agents/author.md`. Turma loads config, scaffolds an OpenSpec
change, and spawns `claude -p` to generate `proposal.md`, `design.md`, and
`tasks.md`. The developer reviews the output and either refines manually or
proceeds to implementation.
