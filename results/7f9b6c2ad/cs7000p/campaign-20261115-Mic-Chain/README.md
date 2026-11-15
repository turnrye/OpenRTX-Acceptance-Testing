# Mic chain campaign, cs7000p model, OpenRTX 7f9b6c2ad

The comparison of every firmware variant with the baseline: [REPORT.md](REPORT.md).
How the emulator bench works and what each metric means: [METHODS.md](METHODS.md).
Tables in `data/`, charts in `charts/`, provenance in `run.json`.

Each variant is an ordinary run of `acceptance/m17/mic-chain.robot` on the emulator bench:

| variant | run |
|---|---|
| baseline | [20261115-212522-emulator-Mic-Chain](../20261115-212522-emulator-Mic-Chain/) |
| prime_dc | [20261115-212557-emulator-Mic-Chain](../20261115-212557-emulator-Mic-Chain/) |
| keep_sum | [20261115-212634-emulator-Mic-Chain](../20261115-212634-emulator-Mic-Chain/) |
| hpf | [20261115-212710-emulator-Mic-Chain](../20261115-212710-emulator-Mic-Chain/) |
| agc | [20261115-212747-emulator-Mic-Chain](../20261115-212747-emulator-Mic-Chain/) |
| full | [20261115-212824-emulator-Mic-Chain](../20261115-212824-emulator-Mic-Chain/) |
| agc_slow | [20261115-212901-emulator-Mic-Chain](../20261115-212901-emulator-Mic-Chain/) |
| agc_mid | [20261115-212938-emulator-Mic-Chain](../20261115-212938-emulator-Mic-Chain/) |
