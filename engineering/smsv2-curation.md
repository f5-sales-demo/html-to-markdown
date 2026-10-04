# SMSv2 content curation analysis

This analysis and the captured inputs belong to engineering evidence, outside the
consumer corpus. The publication policy represents the requested current-only
editorial boundary; it does not assert that upstream has already removed these
products or stopped supporting existing deployments.

The input snapshot is `content-20261004T143103Z`, 900 documents across four sources.
Its receipt SHA-256 is
`4b57ea12a420c8c9fb206075d9bfa3edd565ad7c29c0880d5983f47cc374b482`.
The first 104 matches were examination candidates. Broader candidate detection
also examines unversioned Secure Mesh, generic fleets, and orchestration terms;
these are never sufficient retirement evidence.

## Immutable specification contract

The specification release is `api-specs-enriched v10.0.1`, commit
`ec431fb7909aae6a211468542c1e74c04122a56a`.
The archive SHA-256 is
`c7ed97855749306619296dbf125e510298f859098fa6ed84c34bca39537019ec`.
The SMSv2 contract version is `7.0.0`; its SHA-256 is
`2dd4d33693c9e228bfb304d7558a2f50ca9cc0146407f8fb9b5f0ca7d7c56f3c`.
The evidence receipt SHA-256 is
`e9bd60f46acd2a003010cbf20d0581af364cce0486f8a7eefc9eb85562ab0bff`.
The manifest, contract, evidence receipt, source inventory and captured Markdown
are committed beside the policy and verified by the offline loader.

The canonical configuration is `securemesh_site_v2` in namespace `system`, with
collection `/api/config/namespaces/{namespace}/securemesh_site_v2s` and item
`/api/config/namespaces/{namespace}/securemesh_site_v2s/{name}`. Create, read,
replace and delete operate on this owner object. Provider branches and bootstrap
contracts must be respected individually; a working AWS deployment is not proof
of Azure, GCP, KVM, or another provider's runtime acceptance.

For AWS the contract distinguishes the F5 owner configuration and runtime from
AWS infrastructure. AWS owns EC2 instances, ENIs, TGW, TGW Connect, GRE endpoints,
BGP inside CIDRs and autonomous system numbers. F5 owns SMSv2 configuration,
runtime health, BGP peers/routes, simplified routes and upgrade observation.
Customer-managed Terraform cloud resources remain valid current content.
Cloud VPC/VNet connectivity and TGW integration are not retired merely because
older F5 cloud-site orchestration also used them.

AWS `not_managed.node_list[].interface_list[]` identifies interfaces by node and
normalized MAC. Configured interfaces require observed nonempty guest device
names; guessing `eth0`/`eth1` is prohibited by the contract. Initial primary
interface settings become immutable after registration. The contract's accepted
AWS discovery/rebuild configuration requires one non-HA node with one SLO and one
SLI interface. Its historical three-node runtime evidence is a separate scope,
not a general permission to widen the current configuration contract.

Bootstrap sequences create the site, issue a site-bound JWT token in `system`,
retrieve a provider-specific cloud-init template, deploy and register. Token
material is sensitive, opaque and deployment-bound. AWS native preboot acceptance
and Azure API-only evidence have different runtime qualifications. GCP deployment
permissions are customer provisioning permissions, separate from a VM's runtime
service account, as explicitly explained by captured article `K000163093`.

Lifecycle operations include status, available-target discovery, prechecks,
asynchronous software/OS upgrade and upgrade-status observation. Preserve site
configuration and provider infrastructure ownership through these operations.
The current captured deployment guides explain provider VM creation, interfaces,
subnets, routing, registration and inspection. The current registration and
firewall references explain node provisioning and RE connectivity. These
procedures survive the curation boundary without replacement instructions.

## Editorial retirement boundary

The explicitly retired subjects are `securemesh_site` (SMSv1), `aws_vpc_site`,
`aws_tgw_site`, `azure_vnet_site`, `gcp_vpc_site` and fleet-based CE configuration.
The captured official Fleet guide explicitly calls its workflow legacy; captured
cloud creation guides explicitly orchestrate cloud resources; captured VMware
and AWS manual v1 guides identify their workflows as legacy. Their aliases,
permission guides, examples, console actions, migrations and comparisons are
excluded under the user-requested boundary. The pinned catalog contains 39 site
operations plus six Fleet operations; runtime mapping excludes all 45.

The current FAQ includes an `Existing CE Site models` section that recommends
retired orchestration and v1 providers. This entire section is removed. Its
feature-comparison section is also removed. Retained FAQ facts describe SMSv2
providers, registration, HA and interfaces. Mixed paragraphs are indivisible;
no text is renamed from SMSv1 to SMSv2. A tutorial whose prerequisites depend on
retired deployment is omitted even when its load-balancing or application
paragraphs are valid. AppStack product and independently applicable managed
Kubernetes content remain within scope.

## Structural and media decisions

CommonMark structural spans retain original bytes and distinguish heading
occurrences, sections, paragraphs, nested list items, tables/rows, fenced code,
indented code, HTML and reference definitions. Committed removal rules bind
canonical source, input body hash, structural location and block hash. Reviewed
output hashes account for existing renderer whitespace normalization. Changed,
ambiguous, overlapping or incomplete guards omit a page with audit evidence.

Candidate media is inventoried by actual bytes. Static contact sheets, SVG
renderings and all frames of the four RE-selection animations were inspected.
Reviewed obsolete diagrams, CE bootstrap/registration UI and fleet screenshots
are marked remove. Captions are removed as separate complete guarded blocks.
A page with unreviewed, changed or unverifiable remote/video media is omitted.
Pixels are never modified by topic curation. Original privacy/media reviews still
apply to community source extraction.

Dependencies are closed to a fixed point. Curated references to removed subjects
receive no replacement, API fallback, compatibility page or tombstone. Topic
curation runs before API migration and final publication validation. Detailed
exclusions, original mappings and findings are in `curation-audit.json`, uploaded
as a separate CI artifact. Consumer reports contain retained measurements and
neutral verification data. Manifest schema v2 and the closed snapshot release
asset set remain unchanged. Earlier immutable snapshots are preserved.
