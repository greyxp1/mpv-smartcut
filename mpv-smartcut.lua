local options = require "mp.options"
local utils = require "mp.utils"

local opts = {
    backend = "mpv-smartcut-backend",
    cut_key = "c",
    cancel_key = "C",
    output_prefix = "cut_",
}
options.read_options(opts, "mpv-smartcut")

local mark
local task
local started
local progress_path
local job_serial = 0
local overlay = mp.create_osd_overlay("ass-events")

local function show_status(text)
    overlay.data = "{\\an8\\fs22\\bord2}" .. text
    overlay:update()
end

local function notify(message, duration)
    mp.msg.info(message)
    mp.osd_message(message, duration or 3)
end

local function read_progress()
    local file = io.open(progress_path, "r")
    if not file then return nil end
    local completed, total = (file:read("*l") or ""):match("^(%d+),(%d+)$")
    file:close()
    if total and total ~= "0" then return math.floor(completed * 100 / total) end
end

local progress_timer = mp.add_periodic_timer(0.25, function()
    local percent = read_progress()
    show_status("Smart cutting" .. (percent and (" • " .. percent .. "%") or "") .. " • " ..
        math.floor(mp.get_time() - started) .. "s • " .. opts.cancel_key .. " cancel")
end, true)

local function choose_output(input)
    local directory, filename = utils.split_path(input)
    local stem, extension = filename:match("^(.*)(%.[^.]*)$")
    if not stem then stem, extension = filename, ".mkv" end
    local name, index = opts.output_prefix .. stem .. extension, 1
    while utils.file_info(utils.join_path(directory, name)) do
        index = index + 1
        name = opts.output_prefix .. stem .. "_" .. index .. extension
    end
    return directory, name
end

local function finish_job(output, kept, success, result, error_message)
    task = nil
    progress_timer:kill()
    overlay:remove()

    if result and result.killed_by_us then
        notify("Cut cancelled", 2)
    elseif success and result.status == 0 then
        notify("Cut complete: " .. output, 5)
    else
        local detail = result and (result.stderr ~= "" and result.stderr or result.error_string)
            or error_message or "unknown error"
        detail = detail:gsub("%s+$", ""):sub(-500)
        local failed = not (result and result.status == 2)
        mp.msg.error((failed and "Cut failed: " or "") .. detail)
        mp.osd_message(failed and "Cut failed: " .. detail or "Cut completed but could not be published: " .. kept, 8)
    end
end

local function start_cut(first, second)
    local input = mp.get_property("path")
    if not input or mp.get_property_bool("demuxer-via-network", false) then
        notify("Cut failed: input is not a local file", 5)
        return
    end
    if first == second then
        notify("Cut cancelled: empty selection", 4)
        return
    end

    job_serial = job_serial + 1
    local directory, output_name = choose_output(input)
    local job_directory = utils.join_path(directory, ".smartcut-" .. utils.getpid() .. "-" .. job_serial)
    local output = utils.join_path(directory, output_name)
    local kept = utils.join_path(job_directory, output_name)
    progress_path = utils.join_path(job_directory, "progress")

    started = mp.get_time()
    show_status("Smart cutting • 0s • " .. opts.cancel_key .. " cancel")
    progress_timer:resume()

    task = mp.command_native_async({
        name = "subprocess",
        args = {
            opts.backend,
            input,
            string.format("%.9f", math.min(first, second)),
            string.format("%.9f", math.max(first, second)),
            job_directory,
            output,
        },
        playback_only = false,
        capture_stderr = true,
    }, function(success, result, error_message)
        finish_job(output, kept, success, result, error_message)
    end)
end

local function select_cut_point()
    if task then
        notify("A cut is already processing")
        return
    end

    local position = mp.get_property_number("time-pos")
    if not position then
        notify("Cut failed: no video timestamp", 5)
        return
    end

    if not mark then
        mark = position
        show_status("Cut start " .. mp.get_property_osd("time-pos/full") .. " • " .. opts.cut_key .. " end • " ..
            opts.cancel_key .. " cancel")
        return
    end

    start_cut(mark, position)
    mark = nil
end

local function cancel_cut()
    if task then
        mp.abort_async_command(task)
    elseif mark then
        mark = nil
        overlay:remove()
        notify("Selection cancelled", 2)
    end
end

mp.add_forced_key_binding(opts.cut_key, "mpv-smartcut-select", select_cut_point)
mp.add_forced_key_binding(opts.cancel_key, "mpv-smartcut-cancel", cancel_cut)

mp.register_event("file-loaded", function()
    if not task then
        mark = nil
        overlay:remove()
    end
end)
