# Proposal: turma init

## Summary

Implement the `turma init` command to prepare a project directory for use with
Turma. This is the first real feature in the CLI — currently `turma init` is a
stub that prints a message and exits.

## Motivation

A developer cloning or starting work in a repo that uses Turma needs a single
command to set up their local environment. Today that means manually copying
`turma.example.toml` to `turma.toml` and knowing which entries belong in
`.gitignore`. `turma init` automates this so the first-run experience is one
command.

## User Story

A developer runs `turma init` in a project directory that contains
`turma.example.toml`. The command creates their local `turma.toml`, ensures
`.gitignore` covers Turma-specific local state, and reports what it did. If they
run it again, nothing changes. If `turma.example.toml` is missing, the command
fails with a clear error.
