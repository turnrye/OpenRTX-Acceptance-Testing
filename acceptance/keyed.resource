# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     Transmissions the bench keys itself where it has a PTT line (an audio
...               interface on the radio's accessory jack), and the operator keys where
...               it doesn't, from one instruction as before.

*** Keywords ***
Key Up
    [Documentation]    Key the radio for ${seconds} s through Key Radio, then wait
    ...                ${gap} s so the next transmission is a separate one.
    [Arguments]    ${seconds}    ${gap}=2
    Key Radio
    Wait Seconds    ${seconds}
    Release Radio
    Wait Seconds    ${gap}

Keyed Instruction
    [Documentation]    The operator's instruction for ${count} key-ups of ${seconds} s.
    [Arguments]    ${seconds}    ${count}
    IF    ${count} == 1
        RETURN    Key PTT for ${seconds} s without speaking, release.
    END
    RETURN    Key PTT ${count} times, ${seconds} s each without speaking, with 2 s between.

Capture Keyed FM Transmissions
    [Documentation]    ${count} FM transmissions of ${seconds} s with nothing at the mic, in
    ...                one capture. Returns their measurements, oldest first.
    [Arguments]    ${seconds}=5    ${count}=1
    ${keys}=    Bench Keys The Radio
    IF    ${keys}
        Start Capture
        Wait Seconds    1
        FOR    ${i}    IN RANGE    ${count}
            Key Up    ${seconds}
        END
        ${tx}=    Stop Capture And Measure FM    expected=${count}
    ELSE
        ${instruction}=    Keyed Instruction    ${seconds}    ${count}
        ${tx}=    Capture Radio Transmissions    ${instruction}    expected=${count}
    END
    RETURN    ${tx}

Capture Keyed M17 Transmissions
    [Documentation]    ${count} M17 transmissions of ${seconds} s with nothing at the mic,
    ...                in one capture. Returns their measurements, oldest first.
    [Arguments]    ${seconds}=5    ${count}=1
    ${keys}=    Bench Keys The Radio
    IF    ${keys}
        Start Capture
        Wait Seconds    1
        FOR    ${i}    IN RANGE    ${count}
            Key Up    ${seconds}
        END
        ${tx}=    Stop Capture And Measure M17    expected=${count}
    ELSE
        ${instruction}=    Keyed Instruction    ${seconds}    ${count}
        ${tx}=    Capture Radio M17 Transmissions    ${instruction}    expected=${count}
    END
    RETURN    ${tx}

Capture Keyed Power Steps
    [Documentation]    A 3 s transmission at each power setting in turn, in one capture,
    ...                with the first setting already set. ${mode} is FM or M17.
    [Arguments]    ${settings}    ${mode}=FM
    ${keys}=    Bench Keys The Radio
    IF    not ${keys}
        ${order}=    Evaluate    ", then ".join($settings)
        ${instruction}=    Set Variable
        ...    For each power setting in turn (${order}): set it, key PTT for 3 s without speaking, release.
        IF    $mode == 'M17'
            ${tx}=    Capture Radio M17 Transmissions    ${instruction}    expected=${{len($settings)}}
        ELSE
            ${tx}=    Capture Radio Transmissions    ${instruction}    expected=${{len($settings)}}
        END
        RETURN    ${tx}
    END
    Start Capture
    Wait Seconds    1
    FOR    ${i}    ${setting}    IN ENUMERATE    @{settings}
        IF    ${i} > 0    Operator Does    Set the power to ${setting}
        Key Up    3
    END
    ${tx}=    Run Keyword    Stop Capture And Measure ${mode}    expected=${{len($settings)}}
    RETURN    ${tx}
