# Mic chain campaign, mduv3x0 model, OpenRTX f50482756

The comparison of every firmware variant with the baseline: [REPORT.md](REPORT.md).
How the emulator bench works and what each metric means: [METHODS.md](METHODS.md).
Tables in `data/`, charts in `charts/`, provenance in `run.json`.

f50482756 differs from 7f9b6c2ad (the CS7000-PLUS and bare campaigns) only in CS7000-PLUS ADC code and a gain-macro refactor that leaves the emulated mic chain unchanged.

Each variant is an ordinary run of `acceptance/m17/mic-chain.robot` on the emulator bench:

| variant | run |
|---|---|
| baseline | [20261115-222736-emulator-Mic-Chain](../20261115-222736-emulator-Mic-Chain/) |
| prime_dc | [20261115-223111-emulator-Mic-Chain](../20261115-223111-emulator-Mic-Chain/) |
| keep_sum | [20261115-223442-emulator-Mic-Chain](../20261115-223442-emulator-Mic-Chain/) |
| hpf | [20261115-223814-emulator-Mic-Chain](../20261115-223814-emulator-Mic-Chain/) |
| agc | [20261115-224146-emulator-Mic-Chain](../20261115-224146-emulator-Mic-Chain/) |
| full | [20261115-224519-emulator-Mic-Chain](../20261115-224519-emulator-Mic-Chain/) |
| agc_slow | [20261115-224851-emulator-Mic-Chain](../20261115-224851-emulator-Mic-Chain/) |
| agc_mid | [20261115-225222-emulator-Mic-Chain](../20261115-225222-emulator-Mic-Chain/) |
