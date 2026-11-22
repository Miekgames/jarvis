# Which enabled inbound rules would let any device reach JARVIS's port?
#
# Windows Firewall permits a packet if ANY enabled Allow rule matches it, so a
# rule scoped to the tailnet restricts nothing while a broader rule already
# covers the same port. Finding the broader ones is the whole point: without
# this, the setup script could truthfully report that it added its own rule and
# leave someone believing something false about who can connect.
#
# A rule only applies if *every* one of its filters matches, which is what makes
# the naive version of this check wildly wrong -- several hundred of Windows'
# own rules have LocalPort=Any but are scoped to a particular program, package
# or service, and so cannot carry traffic to a Python process.
#
# This reports the two cases that can, and says so rather than claiming to be
# exhaustive:
#
#   program  the rule names our interpreter specifically (the usual culprit:
#            Windows asks "allow python.exe on your networks?" the first time
#            you run a server, and a yes leaves a rule that allows every port
#            from every address for the life of that interpreter)
#   open     the rule is scoped to no program, package or service at all
#
# In both cases the rule must also cover our port, be TCP or protocol-agnostic,
# and accept connections from any remote address -- a rule already limited to
# some address range is not what we are looking for.
#
# Output: one "kind<TAB>profile<TAB>name" line per rule. Nothing when clean.
param(
    [Parameter(Mandatory = $true)][int]$Port,
    [string]$Exclude = '',
    [string]$Program = ''
)

$ErrorActionPreference = 'Stop'

function Covers-Port($filter, [int]$wanted) {
    foreach ($entry in @($filter.LocalPort)) {
        if ($null -eq $entry) { continue }
        $text = [string]$entry
        if ($text -eq 'Any') { return $true }
        if ($text -match '^\d+$' -and [int]$text -eq $wanted) { return $true }
        if ($text -match '^(\d+)-(\d+)$') {
            if ($wanted -ge [int]$Matches[1] -and $wanted -le [int]$Matches[2]) { return $true }
        }
    }
    return $false
}

$leaf = ''
if ($Program) { $leaf = (Split-Path $Program -Leaf) }

Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True |
    ForEach-Object {
        $rule = $_
        if ($Exclude -and $rule.DisplayName -eq $Exclude) { return }

        $ports = $rule | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
        if (-not $ports) { return }
        if (-not (Covers-Port $ports $Port)) { return }

        # TCP or protocol-agnostic only: a UDP rule cannot carry an HTTP request.
        $protocol = [string]$ports.Protocol
        if ($protocol -ne 'TCP' -and $protocol -ne 'Any') { return }

        $remote = ($rule | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue).RemoteAddress
        if (-not (@($remote) -contains 'Any')) { return }

        $app = $rule | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue
        $service = ($rule | Get-NetFirewallServiceFilter -ErrorAction SilentlyContinue).Service

        $programs = @()
        $package = ''
        if ($app) {
            $programs = @($app.Program) | Where-Object { $_ }
            $package = [string]$app.Package
        }

        # Does it name our interpreter? Compared on the file name, because the
        # stored path's casing and 8.3 form vary and a rule written for
        # python.exe applies whichever way it is spelled.
        if ($leaf) {
            foreach ($p in $programs) {
                if ($p -ne 'Any' -and (Split-Path $p -Leaf) -ieq $leaf) {
                    "program`t$($rule.Profile)`t$($rule.DisplayName)"
                    return
                }
            }
        }

        # Or is it scoped to nothing at all?
        #
        # The Owner test is what separates signal from noise here. Store apps
        # get a per-user AppContainer rule whose application filter reads
        # Program=Any and Package=<empty> -- so by the obvious test, Microsoft
        # Store, Xbox, Solitaire and two dozen others all look like rules that
        # open every port to the world. They are not: each is tied to one
        # packaged app through the rule's Owner SID, which is where that
        # scoping actually lives. Reporting them would have buried the one
        # rule that genuinely matters under 27 that do not.
        $noProgram = ($programs.Count -eq 0) -or ($programs -contains 'Any')
        $noPackage = [string]::IsNullOrEmpty($package) -or $package -eq 'Any'
        $noService = [string]::IsNullOrEmpty($service) -or $service -eq 'Any'
        $noOwner = [string]::IsNullOrEmpty([string]$rule.Owner)
        if ($noProgram -and $noPackage -and $noService -and $noOwner) {
            "open`t$($rule.Profile)`t$($rule.DisplayName)"
        }
    } |
    Sort-Object -Unique
