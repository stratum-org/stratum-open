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
