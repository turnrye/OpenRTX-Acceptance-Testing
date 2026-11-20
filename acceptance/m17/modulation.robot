# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     The modulation pair of the HR_C6000's M17 modem, swept on each band:
...               at every pair a keyed M17 transmission, measured from the SDR capture
...               (outer and inner deviation, their ratio, EVM, decoding). The grid is
...               Mod1 from MOD1_FROM to MOD1_TO of the radio's calibration value, for
...               each Mod2 in MOD2_FRACTIONS of that same Mod1, since some radios
...               calibrate Mod2 to 0. The test picks the decoded pair with the lowest
...               EVM inside the radio file's m17 limits and charts EVM and deviation
...               against Mod1.
...
...               Needs a bench console build (the firmware/m17-trace trace with its
...               console, OpenRTX branch c6000/bench) and console: true in the bench's
...               trace section: the bench sets the radio's mode, frequency, power and
...               M17 modem, and keys it, through the console. Low power: every pair is
...               a transmission into the bench.
Library           Collections
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Open Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${HARDWARE}    dsp=${DSP}
Suite Teardown    Close Bench
Test Tags         per-band    mode:m17    tx    needs:console

*** Variables ***
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
${FIRMWARE}       ${EMPTY}
${HARDWARE}       ${EMPTY}
${DSP}            ${EMPTY}
# The callsign the radio sends, checked in every decoded LSF
${CALLSIGN}       N0CALL
# The grid: 8 Mod1 values x 5 Mod2 values, 3 s each, about 5 min per band
${MOD1_FROM}      0.75
${MOD1_TO}        1.25
${MOD1_STEPS}     8
${MOD2_FRACTIONS}    0,0.2,0.4,0.6,0.8
${SECONDS}        3

*** Test Cases ***
M17 Chip Modem Modulation Sweep
    [Documentation]    M17-MOD-1: the sweep, the pick and the charts on this band.
    Skip Unless Bench Has Radio Console
    Set Up Radio Over Console    M17    callsign=${CALLSIGN}
    Set M17 Modem    CHIP
    Sweep M17 Modulation    ${MOD1_FROM}    ${MOD1_TO}    ${MOD1_STEPS}
    ...    ${MOD2_FRACTIONS}    ${SECONDS}

*** Keywords ***
Skip Unless Bench Has Radio Console
    ${console}=    Bench Has Radio Console
    Skip If    not ${console}    Needs a bench console build and console: true in the bench's trace section
