# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     The M17 microphone chain, on the emulator bench: test signals shaped
...               by the radio file's mic_chain model (oversampling, gain, ADC) go
...               through the OpenRTX Linux emulator's real encodeFunc, codec2 and M17
...               modulator; the decoded voice and the samples codec2 was given (the
...               firmware's pre-codec tap) are measured. The firmware is one variant of
...               an OpenRTX checkout, built with that radio's CONFIG_MIC_* values.
...
...               Limits are the radio file's mic_chain.limits: the target of the
...               microphone-chain change, which today's fixed-gain chain is expected to
...               miss. The method, the signals and the metrics are in
...               docs/emulator-mic-campaign.md, which each run keeps as METHODS.md
...               next to its tables and charts.
...
...               Run it with bench/emulator.args; it needs the emulator bench, so other
...               benches exclude it (needs:emulator).
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Open Emulator Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${VARIANT}
...               hardware=${HARDWARE}    openrtx=${OPENRTX}    build_root=${BUILD_ROOT}
...               speech_dir=${SPEECH_DIR}    takes=${TAKES}    reuse_takes=${REUSE_TAKES}    jobs=${JOBS}
Suite Teardown    Close Bench
Test Tags         needs:emulator    mode:m17    tx

*** Variables ***
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
# <variant>@<OpenRTX commit>, such as baseline@62e065ae5
${FIRMWARE}       ${EMPTY}
# A name from oat.emu_campaign.VARIANTS: baseline, prime_dc, keep_sum, hpf, agc, full, ...
${VARIANT}        ${EMPTY}
${HARDWARE}       ${EMPTY}
# The tester's paths, when the bench file doesn't hold them
${OPENRTX}        ${EMPTY}
${BUILD_ROOT}     ${EMPTY}
${SPEECH_DIR}     ${EMPTY}
${TAKES}          ${EMPTY}
${REUSE_TAKES}    ${EMPTY}
${JOBS}           ${EMPTY}
# The signals; smaller sets make a quick run
${TONE_FREQS}     1000 400
# Empty: -50 to -2 dBFS in 3 dB steps, and 0
${TONE_LEVELS}    ${EMPTY}
${SPEECH_FILES}    ${EMPTY}
${SPEECH_LEVELS}    -40 -30 -20 -12 -6 -1
${SPEECH_SECONDS}    12
${STEP}           3:-30 3:-10 6:-30
${STEP_OVERS}     2
${GATE_NOISE}     -55 -40
# Pink noise at the listener, dBFS RMS of the decoded audio's full scale
${LISTEN_NOISE}    -45 -35

*** Test Cases ***
Static Level Curve
    [Documentation]    1 kHz and 400 Hz tones over the ADC's range: the level into codec2
    ...                and out of it against the input, beside the codec-only reference.
    ...                Nothing may reach full scale before codec2.
    ${m}=    Measure Static Level Curve    ${TONE_FREQS}    ${TONE_LEVELS}
    Mic Chain Should Meet Limit    ${m}    pre_clip_max_percent    max_pre_clip_percent

Speech Level Sweep
    [Documentation]    Harvard sentences from -40 to -1 dBFS peak at the ADC: the active
    ...                level into codec2 should hardly depend on the input level, nothing
    ...                should clip, and intelligibility should stay near the best any
    ...                firmware could reach on the same input (the ideal codec-only
    ...                reference).
    ${m}=    Measure Speech Level Sweep    ${SPEECH_FILES}    ${SPEECH_LEVELS}    ${SPEECH_SECONDS}
    Run Keyword And Continue On Failure
    ...    Mic Chain Should Meet Limit    ${m}    pre_speech_spread_db    max_pre_speech_spread_db
    Run Keyword And Continue On Failure
    ...    Mic Chain Should Meet Limit    ${m}    pre_clip_max_percent    max_pre_clip_percent
    Mic Chain Should Meet Limit    ${m}    stoi_deficit    max_stoi_deficit

Key-up Onset
    [Documentation]    The transient at key-up, the first 100 ms of every speech take,
    ...                before codec2: the DC block's start must not click.
    ${m}=    Measure Key-up Onset    ${SPEECH_FILES}    ${SPEECH_LEVELS}    ${SPEECH_SECONDS}
    Mic Chain Should Meet Limit    ${m}    pre_onset_max_dbfs    max_pre_onset_peak_dbfs

Listening-Condition Intelligibility
    [Documentation]    The speech sweep's decoded audio as a listener hears it at a fixed
    ...                receiver volume, at its own level with pink noise added: STOI
    ...                normalises level, so only here does a quiet talker buried at the
    ...                listener count against the chain. In every cell of input and noise
    ...                level the chain should come within the limit of the codec-only ideal
    ...                reference (the same input, ideally processed to -21 dBFS active).
    ...                Re-scores the sweep's takes; no new emulator runs.
    ${m}=    Measure Listening Intelligibility    ${SPEECH_FILES}    ${SPEECH_LEVELS}
    ...    ${SPEECH_SECONDS}    ${LISTEN_NOISE}
    Mic Chain Should Meet Limit    ${m}    listen_deficit_worst    max_listen_stoi_deficit

AGC Step Response
    [Documentation]    A sustained vowel stepping +20 dB then -20 dB, keyed twice in one
    ...                emulator session; the second over is the steady state. A level
    ...                control should compress the step, settle soon and not overshoot.
    ${m}=    Measure AGC Step Response    ${STEP}    ${STEP_OVERS}
    Run Keyword And Continue On Failure
    ...    Mic Chain Should Meet Limit    ${m}    pre_rise_db    max_pre_step_rise_db
    Run Keyword And Continue On Failure
    ...    Mic Chain Should Meet Limit    ${m}    pre_attack_s    max_pre_attack_s
    Mic Chain Should Meet Limit    ${m}    pre_overshoot_db    max_pre_overshoot_db

Silence Gate
    [Documentation]    Speech bursts and 4 s pauses over a pink noise bed: the noise in a
    ...                pause must not be pumped up as the pause goes on.
    ${m}=    Measure Silence Gate    ${GATE_NOISE}
    Mic Chain Should Meet Limit    ${m}    pre_pause_drift_max_db    max_pre_pause_drift_db
