package protocol

import _ "embed"

// The desktop guide belongs to the separately gated 0.9.0 source family.
// The pinned earlier recovery overlay keeps its original guides and admission.
//
//go:embed hosted-onboarding-v3-desktop.md
var HostedOnboardingV3Desktop []byte

// Native-first updates belong to 0.9.8+; published earlier guides stay immutable.
//
//go:embed hosted-onboarding-v3-native-refresh.md
var HostedOnboardingV3NativeRefresh []byte

// Invitation-driven setup belongs to 0.9.12+; earlier published bytes stay intact.
//
//go:embed hosted-onboarding-v3-invite-setup.md
var HostedOnboardingV3InviteSetup []byte

// Setup feedback belongs to 0.9.13+; earlier published guides stay immutable.
//
//go:embed hosted-onboarding-v3-setup-feedback.md
var HostedOnboardingV3SetupFeedback []byte

// Same-conversation recovery belongs to 0.9.14+; prior guides stay immutable.
//
//go:embed hosted-onboarding-v3-resume.md
var HostedOnboardingV3Resume []byte
