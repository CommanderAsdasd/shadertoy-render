#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Uses code from shadertoy-render by Alex J. Champandard (https://github.com/alexjc/shadertoy-render)
# Copyright (c) 2015, Jasmin Patry
# Copyright (c) 2015, Alex J. Champandard
# Copyright (c) 2015, Vispy Development Team.
# Distributed under the (new) BSD License.

from __future__ import (unicode_literals, print_function)

import re
import sys
import argparse
import time
import datetime
import subprocess
import os.path

import numpy

import vispy
from vispy import app
from vispy import gloo
from vispy import io
from vispy.gloo import gl
from vispy.gloo.util import _screenshot
import vispy.util.keys as keys

import watchdog
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler



vertex = """
#version 120

attribute vec2 position;
void main()
{
    gl_Position = vec4(position, 0.0, 1.0);
}
"""

fragment_template = """
#version 120

uniform vec3      iResolution;           // viewport resolution (in pixels)
uniform float     iGlobalTime;           // shader playback time (in seconds)
uniform vec4      iMouse;                // mouse pixel coords
uniform vec4      iDate;                 // (year, month, day, time in seconds)
uniform float     iSampleRate;           // sound sample rate (i.e., 44100)
uniform sampler2D iChannel0;             // input channel. XX = 2D/Cube
uniform sampler2D iChannel1;             // input channel. XX = 2D/Cube
uniform sampler2D iChannel2;             // input channel. XX = 2D/Cube
uniform sampler2D iChannel3;             // input channel. XX = 2D/Cube
uniform vec3      iChannelResolution[4]; // channel resolution (in pixels)
uniform float     iChannelTime[4];       // channel playback time (in sec)
uniform vec2      iOffset;               // pixel offset for tiled rendering

%s

void main()
{
    mainImage(gl_FragColor, gl_FragCoord.xy + iOffset);
}
"""

preamble_lines = fragment_template.split('\n').index("%s")

error_shader = """
void mainImage( out vec4 fragColor, in vec2 fragCoord )
{
    fragColor = vec4(1.0, 0.0, 1.0, 1.0);
}
"""


# Flushes are here to fix issues when connected to a pipe in Windows, e.g. in mintty or emacs
# See e.g. https://github.com/mintty/mintty/issues/56

def print_msg(msg):
    sys.stderr.flush()
    sys.stdout.write(msg)
    sys.stdout.write("\n")
    sys.stdout.flush()


def print_err(msg):
    sys.stdout.flush()
    sys.stderr.write(msg)
    sys.stderr.write("\n")
    sys.stderr.flush()


def get_idate():
    now = datetime.datetime.now()
    utcnow = datetime.datetime.utcnow()
    midnight_utc = datetime.datetime.combine(utcnow.date(), datetime.time(0))
    delta = utcnow - midnight_utc
    return (now.year, now.month, now.day, delta.seconds)


def noise(resolution=64, nchannels=1):
    size = (resolution, resolution, nchannels)
    return numpy.random.randint(low=0, high=256, size=size).astype(numpy.uint8)


class RenderingCanvas(app.Canvas):

    def __init__(
            self,
            glsl,
            filename,
            size = None,
            position = None,
            t = 0.0,
            interval = 'auto',
            always_on_top = False,
            paused = False,
            tiled_render = False,
			progress_file = None,
            tile_size = 256):

        app.Canvas.__init__(
            self,
            keys = 'interactive',
            size = [tile_size] * 2 if tiled_render else size,
            position = None,
            title = filename,
            always_on_top = always_on_top,
            show = False)

        self._filename = filename
        self._render_size = tuple(size)
        self._tiled_render = tiled_render
        self._profile = False

        # Initialize with a "known good" shader program, so that we can set all
        # the inputs once against it.

        self.program = gloo.Program(vertex, fragment_template % error_shader)
        self.program["position"] = [(-1, -1), (-1, 1), (1, 1), (-1, -1), (1, 1), (1, -1)]
        self.program['iMouse'] = 0.0, 0.0, 0.0, 0.0
        self.program['iSampleRate'] = 44100.0

        for i in range(4):
            self.program['iChannelTime[%d]' % i] = 0.0
        self.program['iGlobalTime'] = t

        self.program['iOffset'] = 0.0, 0.0

        self.activate_zoom()

        self._paused = paused
        self._timer = None
        self._interval = interval

        self._time_start = time.clock()

        if position is not None:
            self.position = position

        self.set_channel_input(noise(resolution=256, nchannels=3), i=0)
        self.set_channel_input(noise(resolution=256, nchannels=1), i=1)

        self.set_shader(glsl)

        if tiled_render:
            self._tile_index = 0
            self._tile_count = ((size[0] + tile_size - 1) // tile_size) * ((size[1] + tile_size - 1) // tile_size)
            self._tile_coord = [0, 0]
            self._tile_size = tile_size
            self._progress_file = progress_file
            self._rendertex = gloo.Texture2D(shape = (tile_size,) * 2 + (4,))
            self._fbo = gloo.FrameBuffer(self._rendertex, gloo.RenderBuffer((tile_size,) * 2))

            # Allocate buffer to hold final image

            self._img = numpy.zeros(shape = self._render_size[::-1] + (4,), dtype = numpy.uint8)

            # Write progress file now so we'll know right away if there are any problems writing to it

            if self._progress_file:
				self.write_img(self._img, self._progress_file)

            self.program['iResolution'] = (self._render_size[0], self._render_size[1], 0.)
            self._timer = app.Timer(interval, connect=self.on_timer, start=True)
        else:
            if not paused:
                self._timer = app.Timer(interval, connect=self.on_timer, start=True)
            self.show()

    def set_channel_input(self, img, i=0):
        tex = gloo.Texture2D(img)
        tex.interpolation = 'linear'
        tex.wrapping = 'repeat'
        self.program['iChannel%d' % i] = tex
        self.program['iChannelResolution[%d]' % i] = img.shape

    def set_shader(self, glsl):
        self._glsl = glsl

    def draw(self):
        if self._glsl:
            fragment = fragment_template % self._glsl
            self._glsl = None

            # Check to see if the shader will compile successfully before we
            # set it. We do this here because the ShaderWatcher runs in a
            # different thread and so can't access the GL context.

            frag_handle = gl.glCreateShader(gl.GL_FRAGMENT_SHADER)
            gl.glShaderSource(frag_handle, fragment)
            gl.glCompileShader(frag_handle)
            status = gl.glGetShaderParameter(frag_handle, gl.GL_COMPILE_STATUS)
            if not status:
                errors = gl.glGetShaderInfoLog(frag_handle)
                errors = self.process_errors(errors)
                print_err("Shader failed to compile:")
                print_err(errors)
            else:
                self.program.set_shaders(vertex, fragment)
            gl.glDeleteShader(frag_handle)

        if not self._paused:
            self.program['iGlobalTime'] = time.clock() - self._time_start

        if self._tiled_render:
            with self._fbo:
                ts = [self._tile_size] * 2

                if self._tile_coord[0] + ts[0] > self._render_size[0]:
                    ts[0] = self._render_size[0] - self._tile_coord[0]

                if self._tile_coord[1] + ts[1] > self._render_size[1]:
                    ts[1] = self._render_size[1] - self._tile_coord[1]

                gloo.clear('black')
                gloo.set_viewport(0, 0, *ts)
                self.program['iOffset'] = self._tile_coord
                self.program.draw()
                img = _screenshot()
                row = self._render_size[1] - self._tile_coord[1] - ts[1]
                col = self._tile_coord[0]
                self._img[row:row + ts[1], col:col + ts[0], :] = img
        else:
            self.program.draw()

    def on_draw(self, event):
        self.draw()

    def on_mouse_press(self, event):
        x, y = event.pos
        imouse = (x, self.size[1] - y)
        imouse += imouse
        self.program['iMouse'] = imouse
        if not self._timer:
            self.update()

    def on_mouse_move(self, event):
        if event.is_dragging:
            x, y = event.pos
            px, py = event.press_event.pos
            imouse = (x, self.size[1] - y, px, self.size[1] - py)
            self.program['iMouse'] = imouse
            if not self._timer:
                self.update()

    def on_key_press(self, event):
        if event.key == "q":
            self.show(False, False)
            self.app.quit()
        elif event.key == "p" or event.key == " ":
            self._paused = not self._paused
            self.update_timer_state()
        elif event.key == "s":
            img = _screenshot()
            self.write_img(img)
        elif event.key == "a":
            print_msg(
                "Size/pos args: --size %dx%d --pos %d,%d" % (
                    self.physical_size[0],
                    self.physical_size[1],
                    self.position[0],
                    self.position[1]))
        elif event.key == "f":
            self._profile = not self._profile
            if self._profile:
                def print_profile(fps):

                    print_msg("%.2f ms/frame" % (1000.0 / float(fps)))
                    return False

                self.measure_fps(1.0, print_profile)
            else:
                self.measure_fps(1.0, False)
            self.update_timer_state()

        elif event.key == keys.LEFT or event.key == keys.RIGHT:
            self._paused = True
            self.update_timer_state()
            step = 1.0 / 60.0
            if keys.ALT in event.modifiers:
                step *= 0.1
                if keys.SHIFT in event.modifiers:
                    step *= 0.1
            else:
                if keys.SHIFT in event.modifiers:
                    step *= 10.0
                if keys.CONTROL in event.modifiers:
                    step *= 100.0

            if event.key == keys.LEFT:
                step *= -1.0

            self.program['iGlobalTime'] += step

            self.print_t()

            self.update()

    def on_timer(self, event):
        if self._tiled_render:
            # update() doesn't call on_draw() if window is hidden under some toolkits,
            # so call draw() directly

            self.draw()

            # update tiles

            self._tile_index += 1

            time_elapsed = time.clock() - self._time_start
            time_per_tile = time_elapsed / self._tile_index
            time_total = time_per_tile * self._tile_count
            time_remain = time_total - time_elapsed

            if self._tile_index == self._tile_count:
                self.write_img(self._img)
                app.quit()
                return

            self._tile_coord[0] += self._tile_size
            if self._tile_coord[0] >= self._render_size[0]:
                self._tile_coord[0] = 0
                self._tile_coord[1] += self._tile_size
                if self._progress_file:
					self.write_img(self._img, self._progress_file)

            print_msg("Tile %d / %d (%.2f%%) %s elapsed, %s remaining, %s total" % \
                      (self._tile_index,
                       self._tile_count,
                       self._tile_index * 100.0 / self._tile_count,
                       str(datetime.timedelta(seconds=round(time_elapsed))),
                       str(datetime.timedelta(seconds=round(time_remain))),
                       str(datetime.timedelta(seconds=round(time_total)))))
        else:
            self.update()

    def on_resize(self, event):
        self.activate_zoom()

    def activate_zoom(self):
        if not self._tiled_render:
            gloo.set_viewport(0, 0, *self.physical_size)
            self.program['iResolution'] = (self.physical_size[0], self.physical_size[1], 0.0)

    def process_errors(self, errors):
        # NOTE (jasminp) Error message format depends on driver. Does this catch them all?
        p = re.compile(r'.*0:(\d+): (.*)')
        linesOut = []
        for line in errors.split('\n'):
            result = p.match(line)
            if result:
                linesOut.append("%s(%d): error: %s" % (self._filename,
                                                       int(result.group(1)) - preamble_lines,
                                                       result.group(2)))
            else:
                linesOut.append(line)
        return '\n'.join(linesOut)

    def print_t(self):
        print_msg("t=%f" % self.program['iGlobalTime'])

    def ensure_timer(self):
        if not self._timer:
            self._timer = app.Timer(self._interval, connect=self.on_timer, start=True)

    def update_timer_state(self):
        if not self._paused:
            self._time_start = time.clock() - self.program['iGlobalTime']
            self.ensure_timer()
        else:
            if self._profile:
                self.ensure_timer()
            else:
                if self._timer:
                    self._timer.stop()
                    self._timer = None

            self.print_t()

    def write_img(self, img, filename = None):
        if filename is None:
            suffix = 0;
            filepat = "screen%d.png"
            while os.path.exists(filepat % suffix):
                suffix = suffix + 1
            filename = filepat % suffix
        io.write_png(filename, img)
        print_msg("Wrote " + filename)


class ShaderWatcher(FileSystemEventHandler):
    def __init__(self, filename, canvas):
        FileSystemEventHandler.__init__(self)
        self._filename = filename
        self._canvas = canvas

    def on_modified(self, event):
        if os.path.abspath(event.src_path) == self._filename:
            print_msg("Updating shader...")

            glsl_shader = open(self._filename, 'r').read()

            self._canvas.set_shader(glsl_shader)
            self._canvas.update()


if __name__ == '__main__':
    vispy.set_log_level('WARNING')

    parser = argparse.ArgumentParser(description='Render a ShaderToy-style shader from the specified file.')
    parser.add_argument('input', type=str, help='Source shader file to load from disk.')
    parser.add_argument(
        '--size',
        type=str,
        default='1280x720',
        help='Width and height of the viewport, e.g. 1920x1080 (string).')
    parser.add_argument(
        '--pos',
        type=str,
        help='Position of the viewport, e.g. 100,100 (string).')
    parser.add_argument('--time', type=float, default=0.0, help="Initial time value.")
    parser.add_argument('--fps', type=float, help="Refresh rate in FPS (float).")
    parser.add_argument('--top', action='store_true', help="Keep window on top.")
    parser.add_argument('--pause', action='store_true', help="Start paused.")
    parser.add_argument('--tiled', action='store_true', help="Do tiled render and quit.")
    parser.add_argument('--tile-size', type=int, default=256, help="Tile size for tiled rendering.")
    parser.add_argument('--progress-file', type=str, help="Save tiled rendering progress to specified PNG file.")

    # TODO (jasminp) Add --resume to resume interrupted tiled render using progress file

    args = parser.parse_args()

    resolution = [int(i) for i in args.size.split('x')]
    position = [int(i) for i in args.pos.split(',')] if args.pos is not None else None

    if args.fps is None or args.fps <= 0.0:
        interval = 'auto'
    else:
        interval = 1.0 / args.fps

    glsl_shader = open(args.input, 'r').read()
    canvas = RenderingCanvas(
                glsl_shader,
                args.input,
                size = resolution,
                position = position,
                t = args.time,
                interval = interval,
                always_on_top = args.top,
                paused = args.pause,
                tiled_render = args.tiled,
				progress_file = args.progress_file,
                tile_size = args.tile_size)

    filepath = os.path.abspath(args.input)

    observer = Observer()
    observer.schedule(ShaderWatcher(filepath, canvas), os.path.dirname(filepath))
    observer.start()

    try:
        canvas.app.run()
    except KeyboardInterrupt:
        pass

    observer.stop()
    observer.join()

# Local Variables:
# python-indent: 4
# indent-tabs-mode: nil
# End:
