"""Built-in neutral name pools and the cost model of the synthetic catalog (U11-04).

Split out of `tools.synth.catalog` for the module line budget. Every name is an invented
or generic word; no real organisation or person is named (U11-04 security notes).
"""

from typing import Final


def _words(text: str) -> tuple[str, ...]:
    return tuple(text.split())


ORG_NAMES: Final = _words(
    "Northwind Harbor Summit Meridian Beacon Cascade Granite Horizon Juniper Keystone "
    "Lakeside Maple Orchard Pinnacle Quarry Redwood Sequoia Tidewater Upland Valley Willow "
    "Aspen Birch Cedar Delta Ember Fjord Glacier Hollow Iris Jasper Kestrel Lantern Mesa "
    "Nimbus Oakridge Prairie Quartz Riverbend Sierra Timber Umber Vista Westgate Yarrow "
    "Zephyr Alder Bluff Canyon Dune Estuary Falcon Grove Heron Inlet Juno Kite Lumen Marsh "
    "Northstar"
)
AREAS: Final = _words(
    "Billing Payments Identity Network Storage Compute Data Analytics Mobile Web Search "
    "Catalog Orders Shipping Inventory Pricing Messaging Email Reporting Security Platform "
    "Cloud Database Partners Logistics Helpdesk Retail Wholesale Finance Payroll Onboarding "
    "Accounts Checkout Content Media Devices Desktop Telephony Warehouse Procurement"
)
FUNCTIONS: Final = _words(
    "Operations Engineering Support Delivery Services Infrastructure Reliability Automation "
    "Monitoring Release Quality Architecture Integration Enablement Response Tooling "
    "Maintenance Hosting Runtime Deployment Provisioning Access Capacity Assurance Build"
)
ADJECTIVES: Final = _words(
    "Amber Azure Bold Brisk Bright Calm Clear Coral Crimson Silver Golden Swift Quiet Rapid "
    "Steady Noble Lucid Vivid Prime Solid Gentle Hidden Iron Jade Keen Lunar Misty Nimble "
    "Olive Polar Quick Royal Sage Scarlet Solar Stellar True Urban Velvet Warm Wild Young "
    "Zesty Arctic Cobalt Dusty Early Fair Grand Hazel"
)
NOUNS: Final = _words(
    "Anchor Arrow Atlas Badger Bridge Comet Compass Condor Crane Dolphin Eagle Engine Fox "
    "Gate Hawk Island Jaguar Kernel Lagoon Ledger Lynx Mariner Meadow Nexus Otter Owl "
    "Panther Pilot Portal Prism Puma Raven Relay Ridge River Rocket Sparrow Spire Stream "
    "Tiger Tower Trail Voyager Wave Wolf Beacon Canvas Harvest Orbit Signal"
)
FIRST_NAMES: Final = _words(
    "Arlen Brisa Corin Dalia Evren Farah Galen Hollis Imara Joren Kaia Lorne Mira Nolan "
    "Orla Perrin Quinn Rhea Soren Talia"
)
LAST_NAMES: Final = _words(
    "Ashdown Brightwater Coldwell Dunmore Elsworth Fenwick Greyholm Hartwell Ivesby Jarrow "
    "Kilbride Lowther Marchbank Northcott Oakes Pembry Quarles Rowntree Stanwick Thorne"
)
# Cost model (the spec leaves it open): median USD by criticality, log-normal sigma 0.4.
RUN_COST_USD: Final = {1: 2_000_000.0, 2: 900_000.0, 3: 350_000.0, 4: 120_000.0}
DOWNTIME_COST_USD: Final = {1: 40_000.0, 2: 12_000.0, 3: 3_000.0, 4: 800.0}

__all__ = [
    "ADJECTIVES",
    "AREAS",
    "DOWNTIME_COST_USD",
    "FIRST_NAMES",
    "FUNCTIONS",
    "LAST_NAMES",
    "NOUNS",
    "ORG_NAMES",
    "RUN_COST_USD",
]
