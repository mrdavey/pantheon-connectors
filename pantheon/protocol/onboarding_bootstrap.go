package protocol

import _ "embed"

// Keep candidate and independently derived recovery inputs self-contained.
// The public guide advances only with its qualified package descriptor.
//
//go:embed hosted-onboarding-v3-pre-connect.md
var HostedOnboardingV3PreConnect []byte

//go:embed hosted-onboarding-v3-setup.md
var HostedOnboardingV3Setup []byte

//go:embed hosted-onboarding-v3-saved-setup.md
var HostedOnboardingV3SavedSetup []byte

//go:embed hosted-onboarding-v3-reload.md
var HostedOnboardingV3Reload []byte

//go:embed bootstrap.py
var OnboardingBootstrap []byte
