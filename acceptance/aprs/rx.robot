# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     APRS on receive (OpenRTX#559): the transmitter sends APRS packets
...               (oat.aprs: AX.25 UI frames as 1200 baud Bell 202 AFSK on FM, checked
...               against Dire Wolf) and messages addressed to the radio's APRS
...               callsign must land in its inbox, once each, and nothing else.
...
...               Until the radio can report its inbox over a data link, the operator
...               reads it: every message carries a short tag ("oat1", "oat2"...) to
...               look for. No audio interface is needed. The radio file decides
...               where APRS runs (mode:aprs on a band).
Library           Collections
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Open Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${HARDWARE}    dsp=${DSP}
Suite Teardown    Close Bench
Test Tags         per-band    mode:aprs    rx

*** Variables ***
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
${FIRMWARE}       ${EMPTY}
${HARDWARE}       ${EMPTY}
${DSP}            ${EMPTY}
# The radio's Settings > APRS > Callsign, with an SSID so the SSID tests
# have one to vary
${APRS_CALLSIGN}    N0CALL-7
# The station the messages come from
${SENDER}         N9OAT
${PATH}           WIDE1-1,WIDE2-1
# The louder tone's peak deviation for the ordinary tests: a 25 kHz channel
${DEVIATION_HZ}    3000
@{DEVIATIONS_HZ}    1500    3000    4500
# More position and status packets than the inbox has entries (32 on
# OpenRTX#559), so storing them would push the message out
${TRAFFIC}        40

*** Keywords ***
Set Up APRS Receive
    Operator Does    Set the radio to APRS on ${FREQUENCY_MHZ} MHz (25 kHz FM), with Settings > APRS > Callsign ${APRS_CALLSIGN}, and open the inbox

Send APRS Packets
    [Documentation]    Send packets with Start Transmitting APRS Packets, wait until
    ...                they're out, then stop the transmitter.
    [Arguments]    @{info}    &{options}
    ${seconds}=    Start Transmitting APRS Packets    @{info}    &{options}
    Wait Seconds    ${{ ${seconds} + 1 }}
    Stop Transmitting

Address Variants
    [Documentation]    The radio's APRS callsign written differently (lower case, SSID
    ...                padded to two digits), the same callsign with another SSID, and
    ...                another station.
    ${call}    ${ssid}=    Evaluate    ($APRS_CALLSIGN.upper().split('-') + ['0'])[:2]
    ${same}=    Evaluate    f"{$call.lower()}-{int($ssid):02d}"
    ${other_ssid}=    Evaluate    f"{$call}-{(int($ssid) + 1) % 16}"
    RETURN    ${same}    ${other_ssid}    OAT9X

*** Test Cases ***
APRS RX Stores A Message For My Callsign
    [Documentation]    APRS-RX-1: a message to the radio's APRS callsign arrives in the
    ...                inbox, from the sender, with its text.
    Set Up APRS Receive
    ${msg}=    APRS Message    ${APRS_CALLSIGN}    oat1 hello from the bench    01
    Send APRS Packets    ${msg}    source=${SENDER}    path=${PATH}    deviation_hz=${DEVIATION_HZ}
    Operator Confirms    The inbox shows one new APRS message from ${SENDER}: "oat1 hello from the bench"

APRS RX Matches The Addressee As An Address
    [Documentation]    APRS-RX-2: the addressee is compared as an AX.25 address, so
    ...                another spelling of the radio's callsign reaches it, and another
    ...                SSID or another station doesn't.
    Set Up APRS Receive
    ${same}    ${other_ssid}    ${other}=    Address Variants
    ${a}=    APRS Message    ${same}    oat2 written ${same}    02
    ${b}=    APRS Message    ${other_ssid}    oat2 for ${other_ssid}    03
    ${c}=    APRS Message    ${other}    oat2 for ${other}    04
    Send APRS Packets    ${a}    ${b}    ${c}    source=${SENDER}    path=${PATH}    deviation_hz=${DEVIATION_HZ}
    Operator Confirms    Of the "oat2" messages, only "oat2 written ${same}" arrived; none for ${other_ssid} or ${other}

APRS RX Stores Copies Of A Message Once
    [Documentation]    APRS-RX-3: the same message sent, retried and relayed by two
    ...                digipeaters is stored once.
    Set Up APRS Receive
    ${msg}=    APRS Message    ${APRS_CALLSIGN}    oat3 sent four times    05
    Send APRS Packets    ${msg}    ${msg}    source=${SENDER}    path=${PATH}    deviation_hz=${DEVIATION_HZ}
    Send APRS Packets    ${msg}    source=${SENDER}    path=DIGI1*,WIDE2-1    deviation_hz=${DEVIATION_HZ}
    Send APRS Packets    ${msg}    source=${SENDER}    path=DIGI1,DIGI2*    deviation_hz=${DEVIATION_HZ}
    Operator Confirms    "oat3 sent four times" is in the inbox once

APRS RX Keeps Messages Through Position Traffic
    [Documentation]    APRS-RX-4: positions and status from other stations aren't
    ...                stored, so ${TRAFFIC} of them, more than the inbox holds, don't
    ...                push a message out.
    Set Up APRS Receive
    ${msg}=    APRS Message    ${APRS_CALLSIGN}    oat4 still here    06
    Send APRS Packets    ${msg}    source=${SENDER}    path=${PATH}    deviation_hz=${DEVIATION_HZ}
    @{traffic}=    Evaluate
    ...    [f"!35{i % 60:02d}.{i:02d}N/090{i % 60:02d}.{i:02d}W-oat4 position {i}" if i % 2 else f">oat4 status {i}" for i in range(${TRAFFIC})]
    Send APRS Packets    @{traffic}    source=OAT4X    path=${PATH}    deviation_hz=${DEVIATION_HZ}    gap_s=0.3
    Operator Confirms    "oat4 still here" is still in the inbox, and none of the "oat4 position" or "oat4 status" packets are

APRS RX Decodes A Full-Size Frame
    [Documentation]    APRS-RX-5: a frame of the full AX.25 size (330 bytes: eight
    ...                digipeaters and a 256-byte information field) decodes. Its message
    ...                text is longer than APRS allows (67), so the inbox may cut it short.
    Set Up APRS Receive
    ${text}=    Evaluate    "oat5 full frame " + "x" * 226
    ${msg}=    APRS Message    ${APRS_CALLSIGN}    ${text}    07
    Send APRS Packets    ${msg}    source=${SENDER}    path=DIGI1*,DIGI2*,DIGI3*,DIGI4*,DIGI5*,DIGI6*,DIGI7*,DIGI8*
    ...    deviation_hz=${DEVIATION_HZ}
    Operator Confirms    A message starting "oat5 full frame xxx" arrived (cut short or not)

APRS RX Across Deviation And Pre-emphasis
    [Documentation]    APRS-RX-6: messages at ${DEVIATIONS_HZ} Hz deviation, each flat
    ...                and with a pre-emphasizing transmitter's 5.3 dB twist, all arrive:
    ...                a strong signal mustn't overflow the demodulator, a weak one or a
    ...                tilted one mustn't be lost.
    Set Up APRS Receive
    ${expected}=    Create List
    FOR    ${dev}    IN    @{DEVIATIONS_HZ}
        FOR    ${twist}    ${label}    IN    0    flat    5.3    emph
            ${text}=    Set Variable    oat6 ${dev} ${label}
            ${msg}=    APRS Message    ${APRS_CALLSIGN}    ${text}
            Send APRS Packets    ${msg}    source=${SENDER}    path=${PATH}    deviation_hz=${dev}    twist_db=${twist}
            Append To List    ${expected}    ${text}
        END
    END
    ${list}=    Evaluate    ", ".join($expected)
    Operator Confirms    All ${{ len($expected) }} "oat6" messages arrived: ${list}
