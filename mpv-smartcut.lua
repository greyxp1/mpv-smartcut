local options = require "mp.options"
local utils = require "mp.utils"

local opts = {
    backend = "mpv-smartcut-backend",
    cut_key = "c",
    cancel_key = "C",
    output_prefix = "cut_",
    quality = "high",
}
options.read_options(opts, "mpv-smartcut")

local mark
local task
local started
local cancelling = false
local shutting_down = false
local temporary_output
local final_output
local progress_path
local overlay = mp.create_osd_overlay("ass-events")

local function format_time(seconds)
    local milliseconds = math.floor((seconds - math.floor(seconds)) * 1000 + 0.5)
    local whole = math.floor(seconds)
    local hours = math.floor(whole / 3600)
    local minutes = math.floor(whole / 60) % 60
    local secs = whole % 60
    return string.format("%02d:%02d:%02d.%03d", hours, minutes, secs, milliseconds)
end

local function show_status(text)
    overlay.data = "{\\an8\\fs22\\bord2}" .. text
    overlay:update()
end

local function hide_status()
    overlay.data = ""
    overlay:update()
end

local function notify(message, duration)
    mp.msg.info(message)
    if not shutting_down then
        mp.osd_message(message, duration or 3)
    end
end

local function remove_file(path)
    if path then
        local removed, error_message = os.remove(path)
        if not removed and utils.file_info(path) then
            mp.msg.warn("Could not remove " .. path .. ": " .. tostring(error_message))
        end
    end
end

local function cleanup_job_files()
    remove_file(temporary_output)
    remove_file(progress_path)
    remove_file(progress_path and progress_path .. ".tmp")
end

local function read_progress()
    if not progress_path then return nil end
    local file = io.open(progress_path, "r")
    if not file then return nil end
    local line = file:read("*l")
    file:close()
    if not line then return nil end
    local completed, total = line:match("^(%d+),(%d+)$")
    completed, total = tonumber(completed), tonumber(total)
    if not completed or not total or total == 0 then return nil end
    return math.min(100, math.floor(completed * 100 / total))
end

local progress_timer = mp.add_periodic_timer(0.25, function()
    if not task then return end
    local elapsed = math.floor(mp.get_time() - started)
    local percent = read_progress()
    local progress = percent and (" • " .. percent .. "%") or ""
    local action = cancelling and "Cancelling" or "Smart cutting"
    show_status(action .. progress .. " • " .. elapsed .. "s • " .. opts.cancel_key .. " cancel")
end)
progress_timer:kill()

local function split_extension(filename)
    local stem, extension = filename:match("^(.*)(%.[^.]*)$")
    if not stem then
        return filename, ".mkv"
    end
    return stem, extension
end

local function choose_output(input)
    local directory, filename = utils.split_path(input)
    local stem, extension = split_extension(filename)
    local suffix = ""
    local index = 1

    while true do
        local output_name = opts.output_prefix .. stem .. suffix .. extension
        local output = utils.join_path(directory, output_name)
        local partial = utils.join_path(
            directory,
            "." .. opts.output_prefix .. stem .. suffix .. ".partial" .. extension
        )
        if not utils.file_info(output) and not utils.file_info(partial) then
            return output, partial
        end
        index = index + 1
        suffix = "_" .. index
    end
end

local function finish_job(success, result, error_message, job_output, job_temporary, job_progress)
    task = nil
    progress_timer:kill()
    remove_file(job_progress)
    remove_file(job_progress .. ".tmp")

    if cancelling then
        remove_file(job_temporary)
        cancelling = false
        temporary_output, final_output, progress_path = nil, nil, nil
        hide_status()
        notify("Cut cancelled", 2)
        return
    end

    if success and result and result.status == 0 then
        local renamed, rename_error = os.rename(job_temporary, job_output)
        temporary_output, final_output, progress_path = nil, nil, nil
        hide_status()
        if renamed then
            notify("Cut complete: " .. job_output, 5)
        else
            mp.msg.error("Could not finalize cut: " .. tostring(rename_error))
            notify("Cut completed but could not be renamed: " .. job_temporary, 8)
        end
        return
    end

    remove_file(job_temporary)
    temporary_output, final_output, progress_path = nil, nil, nil
    hide_status()

    local detail = error_message
    if not detail and result then
        if result.stderr and result.stderr ~= "" then
            detail = result.stderr
        else
            detail = result.error_string
        end
    end
    detail = detail and tostring(detail):gsub("%s+$", "") or "unknown error"
    if #detail > 500 then detail = detail:sub(-500) end
    mp.msg.error("Cut failed: " .. detail)
    notify("Cut failed: " .. detail, 8)
end

local function start_cut(first, second)
    local input = mp.get_property("path")
    if not input or mp.get_property_bool("demuxer-via-network", false) then
        notify("Cut failed: input is not a local file", 5)
        return
    end

    local cut_start = math.min(first, second)
    local cut_end = math.max(first, second)
    if cut_end <= cut_start then
        notify("Cut cancelled: empty selection", 4)
        return
    end

    final_output, temporary_output = choose_output(input)
    progress_path = temporary_output .. ".progress"
    local job_output = final_output
    local job_temporary = temporary_output
    local job_progress = progress_path

    started = mp.get_time()
    show_status("Smart cutting • 0s • " .. opts.cancel_key .. " cancel")
    progress_timer:resume()

    task = mp.command_native_async({
        name = "subprocess",
        args = {
            opts.backend,
            input,
            job_temporary,
            "--keep",
            string.format("%.9f,%.9f", cut_start, cut_end),
            "--progress-file",
            job_progress,
            "--quality",
            opts.quality,
        },
        playback_only = false,
        capture_stderr = true,
        capture_size = 1048576,
    }, function(success, result, error_message)
        finish_job(success, result, error_message, job_output, job_temporary, job_progress)
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
        show_status("Cut start " .. format_time(mark) .. " • " .. opts.cut_key .. " end • " ..
            opts.cancel_key .. " cancel")
        return
    end

    local first = mark
    mark = nil
    start_cut(first, position)
end

local function cancel_cut()
    if task then
        cancelling = true
        mp.abort_async_command(task)
        cleanup_job_files()
        show_status("Cancelling…")
    elseif mark then
        mark = nil
        hide_status()
        notify("Selection cancelled", 2)
    end
end

mp.add_forced_key_binding(opts.cut_key, "mpv-smartcut-select", select_cut_point)
mp.add_forced_key_binding(opts.cancel_key, "mpv-smartcut-cancel", cancel_cut)

mp.register_event("file-loaded", function()
    if not task then
        mark = nil
        hide_status()
    end
end)

mp.register_event("shutdown", function()
    shutting_down = true
    if task then
        cancelling = true
        mp.abort_async_command(task)
        cleanup_job_files()
    end
end)
