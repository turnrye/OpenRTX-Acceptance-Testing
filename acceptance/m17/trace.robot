# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     M17 receive, read from the radio's own trace: a trace build of OpenRTX
...               (firmware/m17-trace) reports every frame it decodes on its USB serial
...               port (the bench's trace section), so each frame the transmitter sends
...               is accounted for without an operator: decoded, missed, garbled or
...               repeated, when the link setup data arrived and from where, which
...               frames went to the speaker, and the receive thread's stack.
...
...               Flash a trace build of the firmware under test. The measurements are
...               of the radio's ordinary M17 receive: the trace only copies out what
...               the decoder hands on. Levels are relative to the bench's gain unless
...               ABSOLUTE_LEVELS is true and the calibration has a transmitter level
...               table; relative results compare firmware only within one session on
...               one bench. Tag "long" marks the soak.
Library           Collections
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Open Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${HARDWARE}    dsp=${DSP}
Suite Teardown    Close Bench
Test Tags         per-band    mode:m17    rx    needs:trace

*** Variables ***
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
${FIRMWARE}       ${EMPTY}
${HARDWARE}       ${EMPTY}
${DSP}            ${EMPTY}
# Frames per stream: 10 s, and a shorter one for transmissions joined late
${FRAMES}         250
${LATE_FRAMES}    50
# The LICH carries the LSF in six chunks: two cycles is plenty at the bench level
${LATE_LSF_FRAMES}    12
${SMS_COUNT}      10
${SMS_LENGTH}     60
${BACK_TO_BACK}    5
${BACK_TO_BACK_GAP_S}    0.1
# The sweeps, in dB relative to the bench's gain (or dBm with ABSOLUTE_LEVELS);
# each stops early once fewer than half get through
${ABSOLUTE_LEVELS}    false
${SWEEP_START}    0
${SWEEP_STOP}     -60
${SWEEP_STEP}     1
${SWEEP_FRAMES}    125
${SMS_SWEEP_COUNT}    10
# A level near the threshold for the late-join and soak measurements, in the
# sweep's units: set it from the sweep's 90% point on master
${WEAK_LEVEL}     -30
${SOAK_MINUTES}    10
# Least free receive-thread stack the runs may leave, in bytes
${MIN_FREE_STACK}    128
&{BANDS_SET_UP}

*** Keywords ***
Set Up M17 Trace
    [Documentation]    Once per band: the radio receiving M17 on the band's frequency,
    ...                its USB port on the bench's trace port.
    IF    $BAND not in $BANDS_SET_UP
        Operator Does    Set the radio to M17 on ${FREQUENCY_MHZ} MHz, no encryption, any destination accepted, with its USB port connected to the bench's trace port and nothing else reading it
        M17 Trace Should Be Running
        Set To Dictionary    ${BANDS_SET_UP}    ${BAND}=${True}
    END

Level
    [Documentation]    The option that sets a level in the sweep's units, to pass
    ...                as &{level}.
    [Arguments]    ${level}
    IF    '${ABSOLUTE_LEVELS}'.lower() == 'true'
        ${option}=    Create Dictionary    level_dbm=${level}
    ELSE
        ${option}=    Create Dictionary    level_db=${level}
    END
    RETURN    ${option}

*** Test Cases ***
M17 Trace Every Frame At The Bench Level
    [Documentation]    M17-TRACE-1: at the bench's level every stream frame decodes as
    ...                sent, none garbled, every one played, and the link setup data
    ...                comes from the LSF frame before the first stream frame.
    Set Up M17 Trace
    ${r}=    Score M17 Stream    ${FRAMES}
    Should Be Equal As Integers    ${r}[decoded]    ${r}[sent]    frames decoded
    Should Be Equal As Integers    ${r}[garbled]    0    frames garbled
    Should Be Equal As Integers    ${r}[played]    ${r}[sent]    frames played
    Should Be Equal    ${r}[lsf_from]    F    link setup from
    Should Be Equal As Integers    ${r}[lsf_after_frames]    0    LSF after frames

M17 Trace Joins A Transmission Late
    [Documentation]    M17-TRACE-2: without the LSF frame, as when joining a
    ...                transmission late, the radio rebuilds the link setup data from
    ...                the LICH within ${LATE_LSF_FRAMES} frames and then plays.
    Set Up M17 Trace
    ${r}=    Score M17 Stream    ${LATE_FRAMES}    lsf=False
    Should Be Equal    ${r}[lsf_from]    I    link setup from
    Should Be True    $r['lsf_after_frames'] is not None and $r['lsf_after_frames'] <= ${LATE_LSF_FRAMES}
    ...    LSF rebuilt after ${r}[lsf_after_frames] frames
    Should Be Equal As Integers    ${r}[decoded]    ${r}[sent]    frames decoded

M17 Trace Joins Late Through A Weak Signal
    [Documentation]    M17-TRACE-3: the same at WEAK_LEVEL: how many frames until the
    ...                link setup data is rebuilt, and how much plays. A measurement for
    ...                comparing firmware; no limit.
    Set Up M17 Trace
    ${level}=    Level    ${WEAK_LEVEL}
    ${r}=    Score M17 Stream    ${FRAMES}    lsf=False    &{level}
    Set Test Message    LSF after ${r}[lsf_after_frames] frames, ${r}[played] of ${r}[sent] played (${r}[played_garbled] garbled)

M17 Trace Back-To-Back Transmissions
    [Documentation]    M17-TRACE-4: ${BACK_TO_BACK} transmissions ${BACK_TO_BACK_GAP_S} s
    ...                apart are each heard whole, with their link setup data from the
    ...                start: the radio lets go of one and locks onto the next in time.
    Set Up M17 Trace
    ${r}=    Score M17 Back To Back    ${BACK_TO_BACK}    gap_s=${BACK_TO_BACK_GAP_S}
    Should Be Equal As Integers    ${r}[heard_overs]    ${BACK_TO_BACK}    transmissions heard
    FOR    ${decoded}    IN    @{r}[decoded]
        Should Be Equal As Integers    ${decoded}    ${r}[frames]    frames decoded per transmission
    END

M17 Trace Stream Sensitivity
    [Documentation]    M17-TRACE-5: stream frames decoded, and garbled frames played,
    ...                as the level falls: the levels where 90% and 50% still decode.
    ...                A measurement for comparing firmware; no limit.
    Set Up M17 Trace
    Find M17 Trace Sensitivity    ${SWEEP_START}    ${SWEEP_STOP}    ${SWEEP_STEP}
    ...    frames=${SWEEP_FRAMES}    absolute=${ABSOLUTE_LEVELS}

M17 Trace SMS At The Bench Level
    [Documentation]    M17-TRACE-6: every SMS arrives intact at the bench's level: its
    ...                packet frames in order and its CRC holding.
    Set Up M17 Trace
    ${r}=    Score M17 SMS    ${SMS_COUNT}    ${SMS_LENGTH}
    Should Be Equal As Integers    ${r}[heard]    ${r}[sent]    SMS intact

M17 Trace SMS Sensitivity
    [Documentation]    M17-TRACE-7: SMS intact as the level falls: the levels where 90%
    ...                and 50% still arrive. A measurement for comparing firmware; no
    ...                limit.
    Set Up M17 Trace
    Find M17 Trace Sensitivity    ${SWEEP_START}    ${SWEEP_STOP}    ${SWEEP_STEP}
    ...    frames=${SMS_SWEEP_COUNT}    absolute=${ABSOLUTE_LEVELS}    kind=sms
    ...    length=${SMS_LENGTH}

M17 Trace Soak
    [Documentation]    M17-TRACE-8: ${SOAK_MINUTES} minutes of reception at WEAK_LEVEL,
    ...                which keeps the decoder busiest: the radio keeps receiving, without
    ...                a reset.
    [Tags]    long
    Set Up M17 Trace
    ${level}=    Level    ${WEAK_LEVEL}
    ${frames}=    Evaluate    int(${SOAK_MINUTES} * 60 / 0.04)
    ${r}=    Score M17 Stream    ${frames}    &{level}
    Set Test Message    ${r}[decoded] of ${r}[sent] frames decoded

M17 Trace Receive Stack Has Headroom
    [Documentation]    M17-TRACE-9: over everything this run received, the receive
    ...                thread kept at least ${MIN_FREE_STACK} B of stack free, the radio
    ...                didn't reset and the trace dropped no lines. Runs last.
    Set Up M17 Trace
    M17 Trace Stack Should Have Headroom    ${MIN_FREE_STACK}
