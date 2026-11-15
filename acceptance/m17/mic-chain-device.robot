# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     The M17 microphone chain on two radios at once: both keyed together on
...               two nearby frequencies, captured by one receiver and channelised apart,
...               with speech played from one speaker at equal distance from both mics.
...               Measures the key-up transient and floor, the level, spread and
...               intelligibility of played speech, and the level of live speech; with a
...               fixed-gain firmware it also gives where real speech lands on the ADC.
...               Run once per firmware image (FIRMWARE <variant>@<OpenRTX sha>) and
...               compare: docs/device-mic-campaign.md, and the timed runbook in
...               docs/device-mic-campaign-runbook.md.
...
...               It needs two radios, the receiver and an audio interface driving a
...               speaker (needs:radio-pair, needs:audio); the operator keys both radios.
...               Each radio's results are written to their own folder,
...               bench-Mic-Chain-Device-<radio file>, and exported on their own
...               (scripts/export-run.sh -b).
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Run Keywords
...               Open Radio Pair Bench    ${BENCH}    ${CALIBRATION}    ${RADIO_A}    ${HARDWARE_A}
...               ${RADIO_B}    ${HARDWARE_B}    ${FIRMWARE}    frequency_a_hz=${FREQUENCY_A_HZ}
...               frequency_b_hz=${FREQUENCY_B_HZ}    image_a=${IMAGE_A}    image_b=${IMAGE_B}
...               AND    Set Speaker Volume    ${SPEECH_FILE}    ${SPEECH_SECONDS}
Suite Teardown    Close Bench
Test Tags         needs:radio-pair    needs:audio    mode:m17    tx    manual

*** Variables ***
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
# <variant>@<OpenRTX sha>, such as baseline@7f9b6c2ad: the same for both radios
${FIRMWARE}       ${EMPTY}
${RADIO_A}        radios/cs7000p.yaml
${HARDWARE_A}     ${EMPTY}
${RADIO_B}        radios/mduv3x0.yaml
${HARDWARE_B}     ${EMPTY}
# Empty: each radio file's 70cm band (445.925 and 445.975 MHz)
${FREQUENCY_A_HZ}    ${EMPTY}
${FREQUENCY_B_HZ}    ${EMPTY}
# The flashed image files, recorded by SHA-256 (optional)
${IMAGE_A}        ${EMPTY}
${IMAGE_B}        ${EMPTY}
# OSR_us_000_0010_8k.wav from the Open Speech Repository
${SPEECH_FILE}    ${EMPTY}
${SPEECH_SECONDS}    10
${PLAYBACK_GAINS}    -20 -10 0
# Seconds from continuing (then keying both radios) to the clip starting
${KEY_LEAD_S}     3
${SILENCE_OVERS}    3
${SILENCE_SECONDS}    3
${SENTENCE}       The birch canoe slid on the smooth planks.

*** Test Cases ***
Key-up And Silence
    [Documentation]    Three quiet overs from both radios: the key-up transient (peak of
    ...                the first 100 ms of decoded audio) and the floor.
    Capture Key-up Silence    ${SILENCE_OVERS}    ${SILENCE_SECONDS}

Played Speech
    [Documentation]    The Harvard clip from the speaker at three levels, both radios keyed
    ...                for each: decoded level and its spread, STOI and ESTOI against the
    ...                source, and STOI at a fixed listening volume over pink noise.
    Capture Played Speech    ${SPEECH_FILE}    ${PLAYBACK_GAINS}    ${SPEECH_SECONDS}    ${KEY_LEAD_S}

Live Voice
    [Documentation]    The operator reads one sentence quietly, normally and loudly at their
    ...                usual distance from both mics: decoded level and its spread.
    Capture Live Voice    ${SENTENCE}
