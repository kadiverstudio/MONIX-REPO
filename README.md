# MONIX

MONIX production tooling for World of Mǒna.

## Production adapter

[`tools/mona-production`](tools/mona-production/README.md) validates dialogue and production profiles for the C# **Sorcerer of Two Covens** starter, then builds a portable MONIX project and an offline HTML review. It uses Python 3.10+ and the standard library.

The game, production profile and media live in the separate production kit. Follow the adapter's README to point it at your local game folder. Repository tests use synthetic fixtures and need no game files:

```bash
python3 -m unittest discover -s tools/mona-production/tests -v
```

## Current scope

This repository contains the production adapter. The full MONIX engine, native applications and game have not been imported. Godot .NET/C# owns gameplay; native MONIX Studio opening and game compilation, playback and export still need validation on a supported workstation.

The adapter follows the MONIX project and asset contracts from [`DUMONIX-REPO` at `24f0a43`](https://github.com/kadiverstudio/DUMONIX-REPO/tree/24f0a43addaefee0662d212d334f3d188251af1c/monix). It does not depend on the held Studio, Live or Forge changes in that repository.

MONIX remains an independent multi-language product. Preserve its build systems and keep intentional cross-product integrations explicit.
