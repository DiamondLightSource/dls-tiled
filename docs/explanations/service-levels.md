# Service levels and outages

## What Tiled is for

For DAQ Core, the main purpose of Tiled is **acquisition**. A beamline that is enabled
for Tiled can't run any plans through blueapi without it, so a Tiled outage stops data
collection.

We will try to avoid any downtime to acquisition. Search, the UI and analysis matter,
but when we have to choose, keeping data collection running comes first.

## What happens if Tiled stops working

If Tiled fails, we restore acquisition first and investigate second:

1. **Bring Tiled back with a new, empty database.** blueapi can write to it, so data
   collection continues.
2. **Find and fix the problem** with Tiled and its production database, while
   beamlines keep collecting.
3. **Point Tiled back at the production database**, and move the data collected in the
   meantime into it.

## What works during an outage

While Tiled is running on the temporary database, it holds only what has been collected
since the switch.

| Works                              | Doesn't work until step 3 is finished                  |
| ---------------------------------- | ------------------------------------------------------ |
| Data collection through blueapi    | Analysis that depends on historical data               |
| Scans collected since the switch   | Searching for earlier scans                            |
|                                    | The UI, and anything else that needs the complete data |

Switching to the temporary database doesn't change the production database, so earlier
data is available again once Tiled points back to it.

To report a problem, see [Get help](../how-to/get-help.md).
