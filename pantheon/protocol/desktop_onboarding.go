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
