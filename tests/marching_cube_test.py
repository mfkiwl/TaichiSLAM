#!/usr/bin/env python3
import os, sys
sys.path.insert(0,os.path.dirname(__file__) + "/../")
from taichi_slam.utils.visualization import *
import time
from taichi_slam.mapping import *

if __name__ == "__main__":
    ti.init(arch=ti.cuda, dynamic_index=True)
    render = TaichiSLAMRender(1920, 1080)
    mapping = DenseESDF(texture_enabled=True, 
                max_disp_particles=10000, 
                min_occupy_thres = 1,
                map_scale=[3, 3],
                voxel_size=0.05,
                block_size=16,
                enable_esdf=False,
                max_ray_length=10)
    max_mesh = 1000000
    mesher = MarchingCubeMesher(mapping, max_mesh)
    mapping.init_sphere()
    mesher.generate_mesh(1)
    render.camera_distance = 3
    render.set_particles(mesher.mesh_vertices, mesher.mesh_vertices)
    render.set_mesh(mesher.mesh_vertices, mesher.mesh_colors, mesher.mesh_indices)

    while True:
        try:
            render.rendering()
            time.sleep(0.01)
        except KeyboardInterrupt:
            exit(0)