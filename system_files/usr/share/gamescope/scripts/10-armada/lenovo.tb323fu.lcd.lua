-- Lenovo Legion Y700 Gen 5 (TB323FU) internal LCD panel.
-- Uses the kernel's real modes as-is; no new timings are generated here.

gamescope.config.known_displays.armada_lenovo_tb323fu_lcd = {
	pretty_name = "Lenovo Legion Y700 Gen 5 (TB323FU)",
	matches = function(display)
		return display.device_id == "lenovo-tb323fu"
			and display.internal
			and display.connector == "DSI-1"
			and 6000 or -1
	end,
}
