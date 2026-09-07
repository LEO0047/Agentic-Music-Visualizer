#!/bin/zsh
cd -- "${0:A:h}"
swift tools/audio_route.swift restore
