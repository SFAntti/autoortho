#!/usr/bin/env python

from __future__ import with_statement

import os
import re
import sys
import time
import math
import errno
import pathlib
import threading
import collections

import logging
logging.basicConfig()
log = logging.getLogger('log')
log.setLevel(logging.INFO)


from fuse import FUSE, FuseOSError, Operations, fuse_get_context

import getortho

from xp_udp import DecodePacket, RequestDataRefs
import socket


def deg2num(lat_deg, lon_deg, zoom):
  lat_rad = math.radians(lat_deg)
  n = 2.0 ** zoom
  xtile = int((lon_deg + 180.0) / 360.0 * n)
  ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
  return (xtile, ytile)


class FlightFollower(object):

    tiles = []
    connected = False
    lat = -1
    lon = -1
    alt = -1
    hdg = -1
    spd = -1

    def __init__(self):
        #self.go = getortho.GetOrtho(chunk_threads=16, tile_threads=2, dds_convert=True)
        self.sock = socket.socket(socket.AF_INET, # Internet
                            socket.SOCK_DGRAM) # UDP

        self.sock.settimeout(5.0)
        t = threading.Thread(target=self.start)
        t.start()

    def start(self):
        log.info("Starting Flight Follower thread.")
        RequestDataRefs(self.sock)
        while True:
            try:
                data, addr = self.sock.recvfrom(1024)
            except socket.timeout:
                self.connected = False
                log.debug("Socket timeout.  Reset.")
                RequestDataRefs(self.sock)
                continue

            self.connected = True

            values = DecodePacket(data)

            for k,v in values.items():
                log.debug(f"{k}: {v}")
                
            lat = values[0][0]
            lon = values[1][0]
            alt = values[3][0]
            hdg = values[4][0]
            spd = values[6][0]

            log.debug(f"Lat: {lat}, Lon: {lon}, Alt: {alt}")
            
            self.alt = alt
            self.lat = lat
            self.lon = lon
            self.hdg = hdg
            self.spd = spd


class DSF(object):

    def __init__(self, tc=None):
        #self.go = getortho.GetOrtho(chunk_threads=16, tile_threads=2, dds_convert=True)
        if tc:
            self.tc = tc
        else:
            self.tc = TileCacher()

    def open(self, path, extra_fast=False):
        start_time = time.time()

        log.info(f"DSF: opening {path} ...")
        ter_files = []
        with open(path, encoding='utf-8', errors='ignore') as h:
            ter_files = re.findall("terrain\W?\d+[-_]\d+[-_]\D*\d+\w*\.ter", h.read())
        log.debug(ter_files)

        ter_dir = os.path.join(os.path.dirname(path), "..", "..")

        dds_full_paths = set()

        log.info(f"DSF: found {len(ter_files)} terrain files.  Parsing ...")
        for t in ter_files:
            ter_path = os.path.join(ter_dir, t) 
            log.debug(f"Checking {ter_path}...")
            with open(ter_path) as h:
                dds_files = re.findall("\S*/\d+[-_]\d+[-_]\D*\d+.dds", h.read())
                log.info(f"Found: {dds_files}")
                for dds in dds_files:
                    dds_full_paths.add(
                        os.path.join(os.path.dirname(ter_path), dds)
                    ) 

        log.debug(dds_full_paths)
        num_dds = len(dds_full_paths)
        log.debug(f"DSF: found {num_dds} dds files.  Retrieving...")

        num_chunks = 8
        chunk_size = int(num_dds/num_chunks)
        dds_list = list(dds_full_paths)
        chunked_list = [dds_list[i:i+chunk_size] for i in
                range(len(dds_list))[::chunk_size]] 

        log.debug("Chunked list:")
        log.debug(chunked_list)

        worker_threads = []
        for dds_paths in chunked_list:
            t = threading.Thread(target=self.get_dds,
                    args=(dds_paths,extra_fast))            
            t.start()
            worker_threads.append(t)

        for w in worker_threads:
            w.join()

        end_time = time.time()

        log.info(f"DSF: Retrieved all tiles for {path} in %s seconds" % (end_time - start_time))


    def get_dds(self, dds_files, extra_fast=False):

        for dds in dds_files:
            m = re.match(".*/(\d+)[-_](\d+)[-_](\D*)(\d+).dds", dds)
            if m:
                log.info(f"Found DDS file {dds}: %s " % str(m.groups()))

                if not os.path.exists(dds):
                    log.info(f"DDS file {dds} does not exist.  Create it.")
                    pathlib.Path(dds).touch()

                size = os.path.getsize(dds)
                log.debug(f"FILE SIZE: {size}")

                if size == 0:
                    log.info(f"{dds} Empty DDS, get ortho")
                    row, col, maptype, zoom = m.groups()
                    zoom = int(zoom)
                    if extra_fast:
                        cache_file = self.tc.get_quick(row, col, maptype, zoom, priority=0)
                        #cache_file = self.tc.get_quick(row, col, maptype, zoom, int(zoom)-4, priority=0)
                    else:
                        cache_file = self.tc.get_quick(row, col, maptype, zoom)
                else:
                    log.debug("DDS already exists.")
            else:
                log.debug(f"DDS: {dds} does not match known pattern.")



class TileCacher(object):
    cache_dir = ".cache"
    tile_times = collections.deque(maxlen=5)
    target_zoom = 16

    def __init__(self):
        self.go = getortho.GetOrtho(chunk_threads=24, tile_threads=4)
        if not os.path.exists(self.cache_dir):
            log.info("Creating cache dir.")
            os.makedirs(self.cache_dir)

    def get_quick(self, row, col, map_type, zoom, min_zoom=0, priority=1):
        zoom = int(zoom)
        if not min_zoom:
            min_zoom = zoom - 3

        found = False
        for z in range(zoom, (min_zoom-1), -1):
            cache_file = os.path.join(self.cache_dir, f"{row}_{col}_{map_type}_{z}.dds")

            with self.go.tile_condition:
                #while cache_file in self.go.active_tiles:
                #    log.info(f"{cache_file} is being actively worked on!")
                #    self.go.tile_condition.wait()
                if cache_file in self.go.active_tiles:
                    log.info(f"{cache_file} is being actively worked on.  Be quick, so continue.")
                    continue

                if os.path.exists(cache_file):
                    log.debug(f"Cache HIT!. Found cached object: {cache_file}")
                    found = True
                    break

        if not found:
            cache_file = os.path.join(self.cache_dir, f"{row}_{col}_{map_type}_{min_zoom}.dds")
            log.info(f"Cache MISS.  Retrieving quick tile {cache_file}")
            while cache_file in self.go.active_tiles:
                log.info(f"{cache_file} is being actively worked on!")
                self.go.tile_condition.wait()
                if os.path.exists(cache_file):
                    log.info(f"{cache_file} is ready.")
                    return cache_file

            start_time = time.time()
            self.go.get_quick_tile(int(col), int(row), int(zoom),
                    int(min_zoom), maptype=map_type, outfile=cache_file,
                    priority=priority)
            
            # Calculate moving average of retrieval times
            end_time = time.time()
            tile_time = end_time - start_time

            self.tile_times.append(tile_time)
            avg_time = sum(self.tile_times)/5 
            if avg_time > 2:
                if self.target_zoom >= 12:
                    self.target_zoom -= 1
                log.info(f"Going slow {avg_time}.  Reduce ZL to {self.target_zoom}")
            elif avg_time <= 0.3:
                if self.target_zoom < 18:
                    self.target_zoom += 1
                log.info(f"Going fast {avg_time}.  Increase ZL to {self.target_zoom}")
            else:
                log.info(f"Average tile time {avg_time}.  Using ZL {self.target_zoom}")


        return cache_file

    def get_target(self, row, col, map_type, zoom):
        zoom = int(zoom)
        min_zoom = min(zoom, max(zoom-2, self.target_zoom))

        cache_file = self.get_quick(int(row), int(col), map_type, zoom, min_zoom)
        return cache_file

    def get_background(self, row, col, map_type, zoom):
        #log.info("Disabling background fetch")
        #return
        #zoom = int(zoom)
        #min_zoom = min(zoom, max(zoom-2, self.target_zoom))
        log.info(f"Tile queue size: {self.go.tile_work_queue.qsize()}.  Chunk queue size {self.go.chunk_work_queue.qsize()}")
        cache_file = os.path.join(self.cache_dir, f"{row}_{col}_{map_type}_{zoom}.dds")
        if os.path.exists(cache_file):
            log.debug(f"Cache HIT! Found high quality cached object: {cache_file}")
        else:
            log.info(f"Cache MISS. Background fetch high quality tile {cache_file}")
            self.go.get_background_tile(int(col), int(row), int(zoom), maptype=map_type, outfile=cache_file)

    def get_best(self, row, col, map_type, zoom):
        cache_file = os.path.join(self.cache_dir, f"{row}_{col}_{map_type}_{zoom}.dds")
        if os.path.exists(cache_file):
            log.debug(f"Cache HIT! Found high quality cached object: {cache_file}")
        elif cache_file in self.go.active_tiles:
            log.info(f"Cache MISS. Want high quality tile {cache_file} but it's busy.  Get quick...")
            cache_file = self.get_quick(int(row), int(col), map_type, int(zoom), int(zoom)-2)
        else:
            log.info(f"Cache MISS. Retrieving high quality tile {cache_file}")
            try:
                self.go.get_tile(int(col), int(row), int(zoom), maptype=map_type, outfile=cache_file)
            except Exception as err:
                log.error(err)

        return cache_file

    def get_deadline(self, row, col, map_type, zoom, quick_zoom=0, min_zoom=0, deadline=0.25, priority=5):
        if quick_zoom:
            cache_file = os.path.join(self.cache_dir, f"{row}_{col}_{map_type}_{quick_zoom}.dds")
        else:
            cache_file = os.path.join(self.cache_dir, f"{row}_{col}_{map_type}_{zoom}.dds")

        if os.path.exists(cache_file):
            log.debug(f"DEADLINE Cache HIT! Found cached object: {cache_file}")
            return cache_file
        else:
            log.info(f"Cache MISS. Background fetch high quality tile {cache_file}")
            log.info(f"Active tile size: {len(self.go.active_tiles)}.  Chunk queue size {self.go.chunk_work_queue.qsize()}")
            self.go.get_background_tile(int(col), int(row), int(zoom),
                    quick_zoom=quick_zoom, maptype=map_type, outfile=cache_file, priority=priority)

        deadline_reached = False
        start_time = time.time()
        with self.go.tile_condition:
            
            while cache_file in self.go.active_tiles or not os.path.exists(cache_file):
                self.go.tile_condition.wait(deadline)
                if (time.time() - start_time) > deadline:
                    log.info(f"DEADLINE {deadline} reached. Break loop.")
                    deadline_reached = True
                    break

            log.info("DEADLINE loop exit.")
            #if not deadline_reached:
            if os.path.exists(cache_file) and not deadline_reached:
                log.info(f"DEADLINE Beat deadline!. Found background object: {cache_file}")
                return cache_file
            else:
                log.info(f"DEADLINE reached.  No tile yet.")

        log.warning(f"DEADLINE reached for {cache_file}.  Get quickly instead ...")
        if not min_zoom:
            min_zoom = int(zoom) - 3
        cache_file = self.get_quick(int(row), int(col), map_type, zoom, min_zoom)
        return cache_file



class AutoOrtho(Operations):

    cache_dir = ".cache"

    open_paths = []
    path_condition = threading.Condition()
    path_dict = {}

    def __init__(self, root):
        log.info(f"ROOT: {root}")
        self.dds_re = re.compile(".*/(\d+)[-_](\d+)[-_](\D*)(\d+).dds")
        self.dsf_re = re.compile(".*/\+\d+[-+]\d+.dsf")
        self.root = root
        #self.go = getortho.GetOrtho()
        self.tc = TileCacher()
        self.dsf_parser = DSF(self.tc) 
        self.ff = FlightFollower()
        #self.background_tc = TileCacher()

    # Helpers
    # =======

    def _full_path(self, partial):
        if partial.startswith("/"):
            partial = partial[1:]
        path = os.path.join(self.root, partial)
        return path


    # Filesystem methods
    # ==================

    def access(self, path, mode):
        #log.debug(f"ACCESS: {path}")
        #m = re.match(".*/(\d+)[-_](\d+)[-_](\D*)(\d+).dds", path)
        #if m:
        #    log.info(f"ACCESS: Found DDS file {path}: %s " % str(m.groups()))
        full_path = self._full_path(path)
        if not os.access(full_path, mode):
            raise FuseOSError(errno.EACCES)

    def chmod(self, path, mode):
        full_path = self._full_path(path)
        return os.chmod(full_path, mode)

    def chown(self, path, uid, gid):
        full_path = self._full_path(path)
        return os.chown(full_path, uid, gid)

    def getattr(self, path, fh=None):
        #log.info(f"GETATTR {path}")

        full_path = None
        m = self.dds_re.match(path)
        if m:
            #log.info(f"{path}: MATCH!")
            row, col, maptype, zoom = m.groups()
            log.debug(f"GETATTR: Fetch for {path}: %s" % str(m.groups()))
            row = int(row)
            col = int(col)
            zoom = int(zoom)
            if maptype != "ZL":
                full_path = self._fetch_dds(row, col, maptype, zoom)
                # Store the last checked cache path.
                self.path_dict[path] = full_path
            else:
                log.debug(f"{path} is ZL type. Skip it.")


        if not full_path:
            full_path = self._full_path(path)

        #log.info(f"GETATTR: FH: {fh}")
        st = os.lstat(full_path)
        
        attrs = dict((key, getattr(st, key)) for key in ('st_atime', 'st_ctime',
                    'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid'))

        if m:
            log.info(f"GETATTR: {path} {attrs}")

        return attrs

    def readdir(self, path, fh):
        log.debug(f"READDIR: {path}")
        full_path = self._full_path(path)

        dirents = ['.', '..']
        if os.path.isdir(full_path):
            dirents.extend(os.listdir(full_path))
        for r in dirents:
            yield r

    def readlink(self, path):
        pathname = os.readlink(self._full_path(path))
        if pathname.startswith("/"):
            # Path name is absolute, sanitize it.
            return os.path.relpath(pathname, self.root)
        else:
            return pathname

    def mknod(self, path, mode, dev):
        return os.mknod(self._full_path(path), mode, dev)

    def rmdir(self, path):
        full_path = self._full_path(path)
        return os.rmdir(full_path)

    def mkdir(self, path, mode):
        return os.mkdir(self._full_path(path), mode)

    def statfs(self, path):
        #log.debug(f"STATFS: {path}")
        full_path = self._full_path(path)
        stv = os.statvfs(full_path)
        return dict((key, getattr(stv, key)) for key in ('f_bavail', 'f_bfree',
            'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
            'f_frsize', 'f_namemax'))

    def unlink(self, path):
        return os.unlink(self._full_path(path))

    def symlink(self, name, target):
        return os.symlink(target, self._full_path(name))

    def rename(self, old, new):
        return os.rename(self._full_path(old), self._full_path(new))

    def link(self, target, name):
        return os.link(self._full_path(name), self._full_path(target))

    def utimens(self, path, times=None):
        return os.utime(self._full_path(path), times)

    # File methods
    # ============


    def _fetch_dds(self, row, col, maptype, zoom):

        if not self.ff.connected:
            cache_file = self.tc.get_quick(row, col, maptype, zoom)
            return cache_file

        x, y = deg2num(self.ff.lat, self.ff.lon, int(zoom))

        log.info(f"Empty DDS and we are flying.  Attempt retrieval...")
        log.info(f"HEADING: {self.ff.hdg} LOCATION: {self.ff.lat}x{self.ff.lon} SPEED {self.ff.spd} ms/s, ALT {self.ff.alt}")
        
        near_range = pow(2, (max(12, int(zoom)) - 12))*4

        distance = math.sqrt( 
            pow((x - col), 2) + pow((y - row), 2)
        )

        facing_tile = False
        if 315 <= float(self.ff.hdg) or float(self.ff.hdg) < 45:
            # North
            fly_direction = "north"
            if row <= y: 
                facing_tile = True
        elif 135 <= float(self.ff.hdg) < 225:
            # South
            fly_direction = "south"
            if row >= y: 
                facing_tile = True
        elif 45 <= float(self.ff.hdg) < 135:
            # East
            fly_direction = "east"
            if col >= x: 
                facing_tile = True
        elif 225 <= float(self.ff.hdg) < 315:
            # West
            fly_direction = "west"
            if col <= x:
                facing_tile = True

        if self.ff.spd > 400 and distance <= near_range and self.ff.alt < 4500:
            log.info(f"Going very fast.  Get fast tile {row}_{col}_{maptype}_{zoom}")
            if facing_tile:
                #cache_file = self.tc.get_quick(row, col, maptype, zoom, zoom-2)
                cache_file = self.tc.get_deadline(row, col, maptype, zoom, zoom-2, deadline=0.35)
            else:
                cache_file = self.tc.get_quick(row, col, maptype, zoom)
        elif self.ff.spd > 200 and distance <= near_range and self.ff.alt < 4500:
            log.info(f"Going fast.  Get fast tile {row}_{col}_{maptype}_{zoom}")
            if facing_tile:
                #cache_file = self.tc.get_quick(row, col, maptype, zoom, zoom-1)
                cache_file = self.tc.get_deadline(row, col, maptype, zoom, zoom-1, deadline=1)
                #cache_file = self.tc.get_deadline(row, col, maptype, zoom, zoom-2, deadline=0.25)
            else:
                cache_file = self.tc.get_quick(row, col, maptype, zoom)
        elif self.ff.spd > 200:
            log.info(f"Going fast, tile is far. Get very quick tile {row}_{col}_{maptype}_{zoom}")
            if facing_tile:
                cache_file = self.tc.get_deadline(row, col, maptype, zoom, zoom-2, deadline=0.35)
            else:
                cache_file = self.tc.get_quick(row, col, maptype, zoom)
        elif distance <= near_range:
            log.info(f"Tile is near.  Check direction for tile {row}_{col}_{zoom}")
            cache_file = None

            if facing_tile:
                #cache_file = self.tc.get_best(row, col, maptype, zoom)
                cache_file = self.tc.get_deadline(row, col, maptype, zoom,
                        deadline=4, priority=2)

                # if fly_direction == "north":
                #     log.info(f"Tile is north.  Get best tile {row}_{col}_{zoom}")
                #     self.tc.get_background(row, col-16, maptype, zoom)
                #     self.tc.get_background(row, col+16, maptype, zoom)
                # elif fly_direction == "south":
                #     log.info(f"Tile is south.  Get best tile {row}_{col}_{zoom}")
                #     self.tc.get_background(row, col-16, maptype, zoom)
                #     self.tc.get_background(row, col+16, maptype, zoom)
                # elif fly_direction == "east":
                #     log.info(f"Tile is east.  Get best tile {row}_{col}_{zoom}")
                #     self.tc.get_background(row-16, col, maptype, zoom)
                #     self.tc.get_background(row+16, col, maptype, zoom)
                # elif fly_direction == "west":
                #     log.info(f"Tile is west.  Get best tile {row}_{col}_{zoom}")
                #     self.tc.get_background(row-16, col, maptype, zoom)
                #     self.tc.get_background(row+16, col, maptype, zoom)
            elif self.ff.spd < 2:
                log.info(f"We aren't moving.  Get best tile {row}_{col}_{zoom}")
                #cache_file = self.tc.get_best(row, col, maptype, zoom)
                cache_file = self.tc.get_deadline(row, col, maptype, zoom, deadline=8)
            else: 
                log.info(f"Tile is not in our direction.  Get quick tile {row}_{col}_{zoom}")
                #cache_file = self.tc.get_quick(row, col, maptype, zoom)
                cache_file = self.tc.get_deadline(row, col, maptype, zoom, deadline=1)


        else:
            log.info(f"Tile is far {row}_{col}_{maptype}_{zoom}.  Current position row:{y} col:{x}.  Distance to tile {distance}.  Range is {near_range}")
            if facing_tile:
                #cache_file = self.tc.get_quick(row, col, maptype, zoom)
                #cache_file = self.tc.get_quick(row, col, maptype, zoom, zoom-2)
                cache_file = self.tc.get_deadline(row, col, maptype, zoom, deadline=1.5)
            else:
                cache_file = self.tc.get_quick(row, col, maptype, zoom)

        return cache_file

    def open(self, path, flags):
        h = None

        #log.info(f"OPEN: {path}, {flags}")
        full_path = self._full_path(path)
        log.debug(f"FULL PATH: {full_path}")
       
        go_fast = False
        if self.ff.spd > 400 and self.ff.alt > 4500:
            log.info("Going very very fast.  Work quickly...")
            go_fast = True

        m = self.dsf_re.match(path)
        if m:
            log.debug("DSF match")
            self.dsf_parser.open(full_path, go_fast)
            h = os.open(full_path, flags)
       

        m = self.dds_re.match(path)
        if m:
            cache_file = self.path_dict.get(path)
            log.info(f"Get cached file from dict: {cache_file}.")
            if not cache_file:
                log.warning(f"{path} Not present in accessed file list.")
                row, col, maptype, zoom = m.groups()
                row = int(row)
                col = int(col)
                zoom = int(zoom)
                cache_file = self.tc.get_quick(row, col, maptype, zoom)
                self.path_dict[path] = cache_file

            h = os.open(cache_file, flags)


        if False:
            with self.path_condition:
                #while path in self.open_paths:
                if path in self.open_paths:
                    log.error(f"{path} is already open!!.  Wait ...")
                    self.path_condition.wait(5)
                self.open_paths.append(path)

            size = os.path.getsize(full_path)
            #log.info(f"OPEN: Found DDS file {path}, size {size} : %s " % str(m.groups()))

            if size == 0:

                row, col, maptype, zoom = m.groups()
                row = int(row)
                col = int(col)
                zoom = int(zoom)
                #self._fetch_dds(row, col, maptype, zoom)

                if go_fast:
                    log.warning(f"Going fast!  {path}")
                    #cache_file = self.tc.get_quick(row, col, maptype, zoom, int(zoom)-4)
                    cache_file = self.tc.get_quick(row, col, maptype, zoom, int(zoom)-4)
                else:
                    cache_file = self.tc.get_quick(row, col, maptype, zoom)

                with self.tc.go.tile_condition:
                    while cache_file in self.tc.go.active_tiles:
                        log.info(f"We want to return {cache_file}, but it's actively being retrieved.  Wait...")
                        self.tc.go.tile_condition.wait()

                h = os.open(cache_file, flags)


        if h is None:
            h = os.open(full_path, flags)

        return h

    def create(self, path, mode, fi=None):
        uid, gid, pid = fuse_get_context()
        full_path = self._full_path(path)
        fd = os.open(full_path, os.O_WRONLY | os.O_CREAT, mode)
        os.chown(full_path,uid,gid) #chown to context uid & gid
        return fd

    def read(self, path, length, offset, fh):
        #log.debug(f"READ: {path}")
        #m = re.match(".*/(\d+)[-_](\d+)[-_](\D*)(\d+).dds", path)
        #if m:
        #    log.debug(f"READ: Found DDS file {path}, offset {offset}, length {length} (%s) " % str(m.groups()))
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, length)

    def write(self, path, buf, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.write(fh, buf)

    def truncate(self, path, length, fh=None):
        full_path = self._full_path(path)
        with open(full_path, 'r+') as f:
            f.truncate(length)

    def flush(self, path, fh):
        return os.fsync(fh)

    def release(self, path, fh):
        # try:
        with self.path_condition:
            if path in self.open_paths:
                log.debug(f"RELEASE: {path}")
                self.open_paths.remove(path)
                self.path_condition.notify_all()
        # except:
        #     pass
        return os.close(fh)

    def fsync(self, path, fdatasync, fh):
        return self.flush(path, fh)


def main(mountpoint, root):
    FUSE(AutoOrtho(root), mountpoint, nothreads=True, foreground=True, allow_other=True)


if __name__ == '__main__':
    main(sys.argv[2], sys.argv[1])