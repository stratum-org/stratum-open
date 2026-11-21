# go vet and Clippy on merged PRs

119 merged PRs from 12 projects, each checked out at its head and built, as a review with the check on would. Each issue the programs reported on a line a PR adds was read and marked: right when what it says is true of the code, wrong when the code works as meant (a false alarm), unsure otherwise. 1 more couldn't be read; they're listed under "What didn't run".

| Check | PRs it read | Added lines | Findings | Right | Wrong | Unsure | Didn't build | Past the review's limit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `checks.go_vet` | 60 | 987 | 0 | 0 | 0 | 0 | 0 | 0 (over 300 s) |
| `checks.clippy` | 59 | 2,839 | 0 | 0 | 0 | 0 | 2 | 0 (over 600 s) |

## Build times

The seconds each check took on a PR, downloading the dependencies included. The first PR of each project was built with nothing cached, as on a fresh runner; the others reused what the PRs before them had downloaded and built. The space is the most the project's clone and cargo target folder took after a PR, and the least disk left free.

| Project | Check | PRs | First PR | Median of the others | Slowest | Past the review's limit | Clone and target | Free |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| caddyserver/caddy | go vet | 10 | 62 s | 3 s | 62 s | 0 | 14 MB | 87,530 MB |
| cli/cli | go vet | 10 | 22 s | 2 s | 34 s | 0 | 56 MB | 91,561 MB |
| etcd-io/etcd | go vet | 10 | 35 s | 6 s | 35 s | 0 | 50 MB | 88,361 MB |
| gin-gonic/gin | go vet | 10 | 32 s | 1 s | 32 s | 0 | 3 MB | 88,924 MB |
| go-gorm/gorm | go vet | 10 | 40 s | 1 s | 40 s | 0 | 3 MB | 89,268 MB |
| prometheus/prometheus | go vet | 10 | 34 s | 2 s | 230 s | 0 | 64 MB | 85,101 MB |
| clap-rs/clap | Clippy | 10 | 2 s | 1 s | 3 s | 0 | 148 MB | 89,332 MB |
| launchbadge/sqlx | Clippy | 10 | 28 s | 2 s | 28 s | 0 | 496 MB | 88,303 MB |
| nushell/nushell | Clippy | 10 | 119 s | 14 s | 119 s | 0 | 6,118 MB | 85,486 MB |
| rust-lang/cargo | Clippy | 10 | 43 s | 10 s | 43 s | 0 | 1,480 MB | 87,762 MB |
| tokio-rs/axum | Clippy | 10 | 5 s | 1 s | 7 s | 0 | 229 MB | 88,551 MB |
| tokio-rs/tokio | Clippy | 9 | 3 s | 1 s | 3 s | 0 | 98 MB | 89,374 MB |

## Every issue on added lines, kept or not

| Program | Code | Kept | Issues | Right | Wrong | Unsure |
| --- | --- | --- | --- | --- | --- | --- |
| staticcheck | `SA1019` | no | 1 | 1 | 0 | 0 |

## What didn't run

| PR | Why |
| --- | --- |
| clap-rs/clap#6535 | Not read by Clippy, past its limit of 3 crates: `clap_derive/src/lib.rs`, `clap_mangen/src/lib.rs` |
| tokio-rs/axum#3963 | Clippy couldn't build the crate in `axum-macros` (`info: syncing channel updates for nightly-2026-05-04-x86_64-unknown-linux-gnu`), so its findings there are missing. |
| tokio-rs/axum#3963 | Clippy built nothing in `axum-macros` (exit code 1, `info: syncing channel updates for nightly-2026-05-04-x86_64-unknown-linux-gnu`) |
| tokio-rs/axum#3935 | Clippy couldn't build the crate in `axum-macros` (`error: 'cargo-clippy' is not installed for the toolchain 'nightly-2026-05-04-x86_64-unknown-linux-gnu'.`), so its findings there are missing. |
| tokio-rs/axum#3935 | Clippy built nothing in `axum-macros` (exit code 1, `error: 'cargo-clippy' is not installed for the toolchain 'nightly-2026-05-04-x86_64-unknown-linux-gnu'.`) |
| tokio-rs/tokio#8743 | its diff isn't in the cache: GitHub has no such PR any more, or `fetch` wasn't run for it |

Toolchains: clippy: `clippy 0.1.100 (0ef1bc3860 2026-11-09)`, `clippy 0.1.94 (e408947bfd 2026-03-25)`, `clippy 0.1.97 (8bab26f4f6 2026-07-14)`; go: `go version go1.27.4 linux/amd64`; staticcheck: `staticcheck 2026.2.2 (0.8.2)`.

## The run

Run [44930780011](https://github.com/stratum-org/stratum-open/actions/runs/44930780011) of the Build-check benchmark, on GitHub's free runners (4 CPUs, 16 GB RAM, no GPU), 21 Nov 2026, from `main` at 65923ea.
- **One job per project:** each built its PRs inside a container made from [Dockerfile](../../Dockerfile), with Go 1.27.4, staticcheck 2026.2.2, and Rust 1.100 with Clippy.
- **Projects' own toolchains:** two pin an older one. sqlx's toolchain file asked for Clippy 1.94, and nushell's for 1.97.
- **What it read:** [clean-go.json](../../planted/clean-go.json) and [clean-rust.json](../../planted/clean-rust.json) list 120 merged PRs from 12 projects. tokio-rs/tokio#8743 is gone from GitHub, as a PR and as an issue, so 119 were checked out and built: 60 Go PRs and 59 Rust ones.
- **What the programs were asked for:** go vet ran with its default analyzers and staticcheck with every SA check, the two the check leaves out included. Clippy ran with its `suspicious` group as well as `correctness`.

## How the issues were marked

Every issue a program reported on a line a PR adds, in a file the check read, was read. It's marked right when what it says is true of the code, wrong when the code works as meant (a false alarm), and unsure otherwise. There was one.

## What it shows

- **One issue on 3,826 added lines.** The 60 Go PRs add 987 lines in the files go vet read, and the 59 Rust PRs add 2,839 in the files Clippy read.
- **That issue is staticcheck's SA1019 on prometheus/prometheus#20345.** The PR adds a use of `labels.MetricName`, which Prometheus has deprecated in favour of `model.MetricNameLabel`. That's true, and the code works, which is why the check leaves SA1019 out. It stays out.
- **The checks kept nothing, so no false alarm.** No go vet analyzer, no staticcheck SA check and no Clippy correctness lint fired on an added line.
- **The kept lists stay as they are.** Nothing fired to judge a kept rule by, and no `suspicious` lint fired, so there's no candidate to add.
- **The time a build adds to a review.** In seconds, per PR, downloading the dependencies included:

| Project | Check | First PR, nothing cached | Median of the other PRs | Slowest |
| --- | --- | --- | --- | --- |
| caddyserver/caddy | go vet | 62 | 3 | 62 |
| cli/cli | go vet | 22 | 2 | 34 |
| etcd-io/etcd | go vet | 35 | 6 | 35 |
| gin-gonic/gin | go vet | 32 | 1 | 32 |
| go-gorm/gorm | go vet | 40 | 1 | 40 |
| prometheus/prometheus | go vet | 34 | 2 | 230 |
| clap-rs/clap | Clippy | 2 | 1 | 3 |
| launchbadge/sqlx | Clippy | 28 | 2 | 28 |
| nushell/nushell | Clippy | 119 | 14 | 119 |
| rust-lang/cargo | Clippy | 43 | 10 | 43 |
| tokio-rs/axum | Clippy | 5 | 1 | 7 |
| tokio-rs/tokio | Clippy | 3 | 1 | 3 |

  - **On a fresh runner,** the first PR of each project is what a workflow with nothing cached would wait: half a minute to a minute for these Go projects, and up to two minutes for Rust (nushell, which checked 811 crates).
  - **With caches,** the later PRs reused what the ones before them downloaded and built, as a workflow that caches them would. They took a few seconds (a median of 14 for nushell).
  - **The slowest, prometheus/prometheus#20345 at 230 s, had caches.** It changed `cmd/prometheus`, whose imports go vet hadn't built yet.
  - **No build came near the check's limits:** 300 s for go vet and staticcheck, 600 s for Clippy.
- **Disk:** the largest was nushell's cargo target folder, 6.1 GB, with 85 GB of the runner's disk still free.

## What didn't build

- **tokio-rs/axum#3963 and #3935: the crate in `axum-macros`.** It has a `rust-toolchain.toml` of its own, naming a nightly of 4 May 2026. When Clippy started there, rustup fetched that toolchain (#3963's error line is rustup's download), but not Clippy for it (#3935's is "`cargo-clippy` is not installed for the toolchain"). The step that adds Clippy had run only at the repository's root. So Clippy built nothing there, and the check said it couldn't build the crate, as a review's coverage line would. A review in a workflow that doesn't install Clippy for that toolchain gets the same line. `build_checks.py` now sets up the toolchain in each crate's folder.
- **clap-rs/clap#6535:** it changes five crates, past the check's limit of three. The other two are named in its coverage line.

## What it doesn't show

- **Misses.** These are merged PRs, so the run counts false alarms only, and a check that reports nothing looks clean here whatever it would miss. The kept rules are shown to work only by `tests/test_checks_build.py`. Whether these projects run the same tools in their own CI wasn't checked.
- **Each kept rule's noise.** None of them fired here.
- **Whole files.** Unlike the ShellCheck run, no file was read as if every line were new, since go vet and Clippy report on packages and crates, not on files.
- **A narrow sample:** 12 projects, 10 PRs each, under the corpus's size limit.
