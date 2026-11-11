# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     APRS receive performance, read from the radio's KISS TNC: the
...               transmitter sends numbered test frames and the radio reports every
...               frame it decodes on its serial port (the bench's tnc section), so
...               each one is accounted for without an operator. Frames carry the run
...               tag, so reruns and other traffic don't count.
...
...               The radio's ordinary APRS receive is what's measured: the TNC only
...               copies out what the demodulator and frame decoder hand to the inbox.
...               Levels are relative to the bench's gain unless ABSOLUTE_LEVELS is
...               true and the calibration has a transmitter level table; relative
...               results compare firmware only within one session on one bench.
...               Limits are the radio file's aprs section. Tag "long" marks the soak.
Library           Collections
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Open Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${HARDWARE}    dsp=${DSP}
Suite Teardown    Close Bench
Test Tags         per-band    mode:aprs    rx    needs:tnc

*** Variables ***
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
${FIRMWARE}       ${EMPTY}
${HARDWARE}       ${EMPTY}
${DSP}            ${EMPTY}
# Frames per measurement: the plain check, and each stress case
${FRAMES}         50
${STRESS_FRAMES}    20
# The sensitivity sweep, in dB relative to the bench's gain (or dBm with
# ABSOLUTE_LEVELS); it stops early once fewer than half are heard
${ABSOLUTE_LEVELS}    false
${SWEEP_START}    0
${SWEEP_STOP}     -50
${SWEEP_STEP}     2
${SOAK_FRAMES}    500
&{BANDS_SET_UP}

*** Keywords ***
Set Up APRS TNC
    [Documentation]    Once per band: the radio in APRS mode on the band's frequency
    ...                and its TNC port connected.
    IF    $BAND not in $BANDS_SET_UP
        Operator Does    Set the radio to APRS on ${FREQUENCY_MHZ} MHz (25 kHz FM), with its USB port connected to the bench's TNC port and nothing else reading it
        Set To Dictionary    ${BANDS_SET_UP}    ${BAND}=${True}
    END

Stress Case
    [Documentation]    Send STRESS_FRAMES test frames with these options; if `enforce`,
    ...                fail (at the end of the test) below the stress limit.
    [Arguments]    ${enforce}    &{options}
    ${r}=    Send APRS Test Frames    ${STRESS_FRAMES}    &{options}
    ${min}=    Radio Value    aprs    min_stress_frame_rate
    IF    ${enforce}
        Run Keyword And Continue On Failure    APRS Frames Should Be Heard    ${r}    ${min}
    END
    RETURN    ${r}

*** Test Cases ***
APRS Perf Every Frame Heard At The Bench Level
    [Documentation]    APRS-PERF-1: at the bench's level, nominal deviation and no
    ...                twist, the radio reports every frame (aprs.min_frame_rate),
    ...                exactly as sent, once.
    Set Up APRS TNC
    ${r}=    Send APRS Test Frames    ${FRAMES}
    APRS Frames Should Be Heard    ${r}

APRS Perf Full-Size Frames Survive The TNC
    [Documentation]    APRS-PERF-2: the largest frame (eight digipeaters, 256-byte
    ...                information field), and one full of the bytes KISS escapes (0xC0,
    ...                0xDB), come out of the TNC exactly as sent: the decoder's size
    ...                limit, and the SLIP encoder's escapes across its chunks.
    Set Up APRS TNC
    ${full}=    Send APRS Test Frames    10    kind=full
    Run Keyword And Continue On Failure    APRS Frames Should Be Heard    ${full}
    ${esc}=    Send APRS Test Frames    10    kind=escapes
    APRS Frames Should Be Heard    ${esc}

APRS Perf Sensitivity
    [Documentation]    APRS-PERF-3: the frame rate as the level falls, and the levels
    ...                where 90% and 50% of frames are still heard. Recorded for
    ...                comparison; no limit yet.
    Set Up APRS TNC
    Find APRS Sensitivity    ${SWEEP_START}    ${SWEEP_STOP}    ${SWEEP_STEP}    ${STRESS_FRAMES}
    ...    absolute=${ABSOLUTE_LEVELS}

APRS Perf Twist
    [Documentation]    APRS-PERF-4: the space tone from 9 dB below the mark to 9 dB
    ...                above it, as transmitters with and without pre-emphasis reach
    ...                receivers with and without de-emphasis. Within 6 dB is held to the
    ...                stress limit; 9 dB is recorded.
    Set Up APRS TNC
    FOR    ${twist}    IN    -9    -6    -3    3    6    9
        Stress Case    ${{ abs(${twist}) <= 6 }}    twist_db=${twist}
    END

APRS Perf Deviation
    [Documentation]    APRS-PERF-5: deviation from 1 to 5 kHz: a weak transmitter, the
    ...                usual 3 kHz, and one driving the demodulator hard. 1.5 to 4.5 kHz
    ...                is held to the stress limit.
    Set Up APRS TNC
    FOR    ${dev}    IN    1000    1500    4500    5000
        Stress Case    ${{ 1500 <= ${dev} <= 4500 }}    deviation_hz=${dev}
    END

APRS Perf Short TXDELAY
    [Documentation]    APRS-PERF-6: less preamble before the frame, so the receiver
    ...                must acquire quickly. 100 ms and more is held to the stress limit.
    Set Up APRS TNC
    FOR    ${ms}    IN    150    100    50
        Stress Case    ${{ ${ms} >= 100 }}    txdelay_ms=${ms}
    END

APRS Perf Off Frequency And Clock Error
    [Documentation]    APRS-PERF-7: the carrier 1.5 kHz either side, and the bit rate
    ...                0.5% either side of 1200 baud.
    Set Up APRS TNC
    FOR    ${hz}    IN    -1500    1500
        Stress Case    ${True}    offset_hz=${hz}
    END
    FOR    ${ppm}    IN    -5000    5000
        Stress Case    ${True}    baud_error_ppm=${ppm}
    END

APRS Perf Back To Back
    [Documentation]    APRS-PERF-8: ten frames per transmission, one flag apart, as a
    ...                digipeater or a TNC emptying its queue sends them.
    Set Up APRS TNC
    Stress Case    ${True}    per_transmission=10

APRS Perf Soak
    [Documentation]    APRS-PERF-9: SOAK_FRAMES frames, short and full-size, at the
    ...                bench's level: none lost or corrupted over a long run.
    [Tags]    long
    Set Up APRS TNC
    ${short}=    Evaluate    int(${SOAK_FRAMES}) * 4 // 5
    ${a}=    Send APRS Test Frames    ${short}
    Run Keyword And Continue On Failure    APRS Frames Should Be Heard    ${a}
    ${b}=    Send APRS Test Frames    ${{ int(${SOAK_FRAMES}) - ${short} }}    kind=full
    APRS Frames Should Be Heard    ${b}
