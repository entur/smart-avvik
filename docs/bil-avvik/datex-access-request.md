# DATEX II access request — prepared materials (day-1 action)

Status: **prepared 2026-07-11, not yet submitted.** This is the day-1 parallel
action from the reisevalg-varsel feasibility plan (U1): SVV's real travel-time
feed has **no historical archive**, so every week before access is granted is
travel-time data lost forever. Registration lead time is reportedly weeks —
submit first, investigate in parallel.

## Where to submit

https://www.vegvesen.no/en/fag/technology/open-data/a-selection-of-open-data/what-is-datex/get-access/

(Web form; a user account for the DATEX II services is created on approval.
The data itself is open/NLOD — registration exists for access control and
contact purposes, not licensing.)

## What to fill in

- **Organisation:** Entur AS (org. nr. 917 422 575)
- **Contact:** the Entur owner of this experiment (Team KI-eksperimentering);
  technical contact e-mail: thomas.oldervoll@entur.org
- **Purpose (suggested text):**

  > Entur AS (the Norwegian national public-transport journey planner) is
  > running a pilot on proactive travel-choice notifications: advising
  > commuters on the E18 Lier–Oslo corridor to take the train on mornings when
  > road congestion is abnormal and rail is running normally. We request
  > access to the DATEX II travel-time publication (GetTravelTimeData) for
  > E18 in the Oslo/Viken area to detect abnormal car travel times in near
  > real time, and to the situation publication (GetSituation) for incident
  > context. Expected load: one poll per publication every ~5 minutes from a
  > single backend service. Data will not be redistributed raw; it feeds an
  > aggregated congestion indicator in a pilot notification service.

- **Publications to request:**
  - **GetTravelTimeData** — measured/estimated travel times per road segment,
    ~5-minute update cycle; covers E18 Oslo–Aust-Agder, i.e. the whole
    Lier–Oslo corridor. This is the primary feed (gives *minutes*, which
    hourly Trafikkdata volumes cannot).
  - **GetSituation** — incidents/roadworks/closures from Vegtrafikksentralen;
    context for why a travel time is abnormal, and a guard against nudging
    into a corridor closure.

## Open question to resolve while the form is processed

**Fixed IP:** the registration form historically asks for the IP address(es)
the service will be consumed from.

- Our consumer runs on Entur's GKE golden path (`kix-avvik` workload,
  `ent-kixavvik-dev` / `-prd`). Question for the platform team: **does the GKE
  egress NAT present a stable, allowlistable public IP (or CIDR) per
  environment?** (Cloud NAT with reserved static addresses would qualify;
  auto-allocated NAT IPs can change.)
- If egress IPs are not stable, options: ask SVV whether the account works
  without an IP allowlist (user/password auth may be sufficient), reserve
  static NAT IPs for the cluster, or poll via a small proxy with a reserved
  address. Resolve with the platform team in parallel — do not block the
  submission on it (state the question in the form's free-text field if the
  IP is not yet known).

## After access is granted

- **Start archiving immediately** — there is no historical DATEX archive; the
  backtest window for travel times begins the day polling starts.
- Credentials go to the API as `SVV_DATEX_USER` / `SVV_DATEX_PASSWORD` via the
  same Secret Manager + ExternalSecret convention as the VAPID keypair (never
  a plaintext Helm values entry). The `/reisevalg` DATEX provider ships as a
  stub and activates when these env vars are set; auth is an
  `Authorization: Basic` header built from them (never embedded in a URL).
