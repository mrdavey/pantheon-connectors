// Package protocol contains the one trusted joining artifact shared by server and adapters.
package protocol

import _ "embed"

//go:embed joining-v1.json
var Joining []byte

const JoiningVersion = "pyros.joining.v1"

//go:embed hosted-onboarding.md
var HostedOnboarding []byte

//go:embed hosted-onboarding-v3.md
var HostedOnboardingV3 []byte
