import base64
import time
from multiprocessing.managers import BaseManager
from fastapi import FastAPI, status, Request
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from typing import List
import cv2 as cv
import uvicorn
import os
import logging
import humps
import numpy as np
from starlette.exceptions import HTTPException
from pydantic import conlist

from api.models.config_keys import *
from api.reports import reports_api
from libs.utils.camera_calibration import (get_camera_calibration_path, compute_and_save_inv_homography_matrix,
                                           ConfigHomographyMatrix)
from share.commands import Commands

logger = logging.getLogger(__name__)


class QueueManager(BaseManager): pass


class ProcessorAPI:
    """
    The ProcessorAPI object implements a fastapi application that should allow configuring, starting and stopping processing,
    and viewing the video stream processed by this processor node.

    :param config: Is a ConfigEngine instance which provides necessary parameters.
    :param engine_instance:  A ConfigEngine object which store all of the config parameters. Access to any parameter
        is possible by calling get_section_dict method.
    """

    def __init__(self, config):
        self.config = config
        self._setup_queues()
        self._host = self.config.get_section_dict("API")["Host"]
        self._port = int(self.config.get_section_dict("API")["Port"])
        self._screenshot_directory = self.config.get_section_dict("App")["ScreenshotsDirectory"]
        self.app = self.create_fastapi_app()

    def _setup_queues(self):
        QueueManager.register('get_cmd_queue')
        QueueManager.register('get_result_queue')
        self._queue_host = self.config.get_section_dict("CORE")["Host"]
        self._queue_port = int(self.config.get_section_dict("CORE")["QueuePort"])
        auth_key = self.config.get_section_dict("CORE")["QueueAuthKey"]
        self._queue_manager = QueueManager(address=(self._queue_host, self._queue_port),
                                           authkey=auth_key.encode('ascii'))

        while True:
            try:
                self._queue_manager.connect()
                break
            except ConnectionRefusedError:
                logger.warning("Waiting for core's queue to initiate ... ")
                time.sleep(1)

        logger.info("Connection established to Core's queue")
        self._cmd_queue = self._queue_manager.get_cmd_queue()
        self._result_queue = self._queue_manager.get_result_queue()

    def create_fastapi_app(self):
        os.environ['LogDirectory'] = self.config.get_section_dict("Logger")["LogDirectory"]
        os.environ['HeatmapResolution'] = self.config.get_section_dict("Logger")["HeatmapResolution"]

        class ImageModel(BaseModel):
            image: str

            class Config:
                schema_extra = {
                    'example': {
                        'image': 'data:image/jpg;base64,iVBORw0KG...'
                    }
                }

        class SlackConfig(BaseModel):
            user_token: str
            channel: Optional[str]

            class Config:
                schema_extra = {
                    'example': {
                        'user_token': 'xxxx-ffff...'
                    }
                }

        # Create and return a fastapi instance
        app = FastAPI()

        app.mount("/reports", reports_api)

        @app.exception_handler(RequestValidationError)
        async def validation_exception_handler(request: Request, exc: RequestValidationError):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=jsonable_encoder({"detail": exc.errors(), "body": exc.body}),
            )

        if os.environ.get('DEV_ALLOW_ALL_ORIGINS', False):
            # This option allows React development server (which is served on another port, like 3000) to proxy requests
            # to this server.
            # WARNING: read this before enabling it in your environment:
            # https://medium.com/@stestagg/stealing-secrets-from-developers-using-websockets-254f98d577a0
            from fastapi.middleware.cors import CORSMiddleware
            app.add_middleware(CORSMiddleware, allow_origins='*', allow_credentials=True, allow_methods=['*'],
                               allow_headers=['*'])

        app.mount("/static", StaticFiles(directory="/repo/data/processor/static"), name="static")

        def map_camera(camera_name, config, options):
            camera = config.get(camera_name)
            camera_id = camera.get("Id")
            image = None
            if "withImage" in options:
                dir_path = os.path.join(self.config.get_section_dict("App")["ScreenshotsDirectory"], camera_id)
                image = base64.b64encode(cv.imread(f'{dir_path}/default.jpg'))

            return {
                "id": camera_id,
                "name": camera.get("Name"),
                "videoPath": camera.get("VideoPath"),
                "emails": camera.get("Emails"),
                "violationThreshold": camera.get("ViolationThreshold"),
                "notifyEveryMinutes": camera.get("NotifyEveryMinutes"),
                "dailyReport": camera.get("DailyReport"),
                'dailyReportTime': camera.get('DailyReportTime'),
                "image": image
            }

        def map_area(area_name, config):
            area = config.get(area_name)

            return {
                "id": area.get("Id"),
                "name": area.get("Name"),
                "cameras": area.get("Cameras"),
                "notifyEveryMinutes": area.get("NotifyEveryMinutes"),
                "emails": area.get("Emails"),
                "occupancyThreshold": area.get("OccupancyThreshold"),
                "violationThreshold": area.get("ViolationThreshold"),
                "dailyReport": area.get('DailyReport'),
                'dailyReportTime': area.get('DailyReportTime')
            }

        def map_to_area_file_format(area: AreaConfigDTO):
            return dict(
                {
                    'Id': area.id,
                    'Name': area.name,
                    'Cameras': area.cameras,
                    'NotifyEveryMinutes': str(area.notifyEveryMinutes),
                    'Emails': area.emails,
                    'OccupancyThreshold': str(area.occupancyThreshold),
                    'ViolationThreshold': str(area.violationThreshold),
                    "DailyReport": str(area.dailyReport),
                    'DailyReportTime': area.dailyReportTime
                }
            )

        def map_to_camera_file_format(camera: SourceConfigDTO):
            return dict(
                {
                    'Name': camera.name,
                    'VideoPath': camera.videoPath,
                    'Id': camera.id,
                    'Emails': camera.emails,
                    'Tags': camera.tags,
                    'NotifyEveryMinutes': str(camera.notifyEveryMinutes),
                    'ViolationThreshold': str(camera.violationThreshold),
                    'DistMethod': camera.distMethod,
                    'DailyReport': str(camera.dailyReport),
                    'DailyReportTime': camera.dailyReportTime
                }
            )

        def map_config(config, options):
            cameras_name = [x for x in config.keys() if x.startswith("Source")]
            areas_name = [x for x in config.keys() if x.startswith("Area")]
            return {
                "host": config.get("API").get("Host"),
                "port": config.get("API").get("Port"),
                "cameras": [map_camera(x, config, options) for x in cameras_name],
                "areas": [map_area(x, config) for x in areas_name]
            }

        def map_to_config_file_format(config_dto):
            config_dict = dict()
            for count, camera in enumerate(config_dto.cameras):
                config_dict["Source_" + str(count)] = map_to_camera_file_format(camera)
            for count, area in enumerate(config_dto.areas):
                config_dict["Area_" + str(count)] = map_to_area_file_format(area)
            return config_dict

        def extract_config(config_type='all'):
            sections = self.config.get_sections()
            if config_type == 'cameras':
                sections = [x for x in sections if x.startswith("Source")]
            elif config_type == 'areas':
                sections = [x for x in sections if x.startswith("Area")]
            config = {}
            
            for section in sections:
                config[section] = self.config.get_section_dict(section)
            return config

        def verify_path(base, camera_id):
            dir_path = os.path.join(base, camera_id)
            if not os.path.exists(dir_path):
                raise HTTPException(status_code=404, detail=f'The camera: {camera_id} does not exist')
            return dir_path

        def update_config_file(config_dict):
            logger.info("Updating config...")
            self.config.update_config(config_dict)
            self.config.reload()

        def restart_processor():
            logger.info("Restarting video processor...")
            self._cmd_queue.put(Commands.STOP_PROCESS_VIDEO)
            stopped = self._result_queue.get()
            if stopped:
                self._cmd_queue.put(Commands.PROCESS_VIDEO_CFG)
                started = self._result_queue.get()
                if not started:
                    logger.info("Failed to restart video processor...")
                    return False
            return True

        def write_user_token(token):
            logger.info("Writing user access token")
            with open("slack_token.txt", "w+") as slack_token:
                slack_token.write(token)

        def enable_slack(token_config):
            write_user_token(token_config.user_token)
            logger.info("Enabling slack notification on processor's config")
            config_dict = dict()
            config_dict["App"] = dict({"EnableSlackNotifications": "yes", "SlackChannel": token_config.channel})
            success = update_and_restart_config(config_dict)

            return handle_config_response(config_dict, success)

        def is_slack_configured():
            if not os.path.exists('slack_token.txt'):
                return False
            with open("slack_token.txt", "r") as user_token:
                value = user_token.read()
                if value:
                    return True
                return False

        def add_slack_channel_to_config(channel):
            logger.info("Adding slack's channel on processor's config")
            config_dict = dict()
            config_dict["App"] = dict({"SlackChannel": channel})

            success = update_and_restart_config(config_dict)
            return handle_config_response(config_dict, success)

        def handle_config_response(config, success):
            if not success:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=jsonable_encoder({
                        'msg': 'Failed to restart video processor',
                        'type': 'unknown error on the config file',
                        'body': humps.decamelize(config)
                    })
                )
            return JSONResponse(content=humps.decamelize(config))

        def get_areas():
            config = extract_config(config_type='areas')
            return [map_area(x, config) for x in config.keys()]

        def reestructure_areas(config_dict):
            """Ensure that all [Area_0, Area_1, ...] are consecutive"""
            area_names = [x for x in config_dict.keys() if x.startswith("Area")]
            area_names.sort()
            for index, area_name in enumerate(area_names):
                if f'Area_{index}' != area_name:
                    config_dict[f'Area_{index}'] = config_dict[area_name]
                    config_dict.pop(area_name)
            return config_dict

        def reestructure_cameras(config_dict):
            """Ensure that all [Source_0, Source_1, ...] are consecutive"""
            source_names = [x for x in config_dict.keys() if x.startswith("Source")]
            source_names.sort()
            for index, source_name in enumerate(source_names):
                if f'Source_{index}' != source_name:
                    config_dict[f'Source_{index}'] = config_dict[source_name]
                    config_dict.pop(source_name)
            return config_dict

        def delete_camera_from_areas(camera_id, config_dict):
            areas = {key: config_dict[key] for key in config_dict.keys() if key.startswith("Area")}
            for key, area in areas.items():
                cameras = area['Cameras'].split(',')
                if camera_id in cameras:
                    cameras.remove(camera_id)
                    if len(cameras) == 0:
                        logger.warning(f'After removing the camera "{camera_id}", the area "{area["Id"]} - {area["Name"]}" was left with no cameras and deleted')
                        config_dict.pop(key)
                    else:
                        config_dict[key]['Cameras'] = ",".join(cameras)

            config_dict = reestructure_areas(config_dict)

            return config_dict

        def get_cameras(options):
            config = extract_config(config_type='cameras')
            return [map_camera(x, config, options) for x in config.keys()]

        def update_and_restart_config(config_dict):
            update_config_file(config_dict)

            # TODO: Restart only when necessary, and only the threads that are necessary (for instance to load a new video)
            success = restart_processor()
            return success

        @app.get("/process-video-cfg")
        async def process_video_cfg():
            logger.info("process-video-cfg requests on api")
            self._cmd_queue.put(Commands.PROCESS_VIDEO_CFG)
            logger.info("waiting for core's response...")
            result = self._result_queue.get()
            return result

        @app.get("/stop-process-video")
        async def stop_process_video():
            logger.info("stop-process-video requests on api")
            self._cmd_queue.put(Commands.STOP_PROCESS_VIDEO)
            logger.info("waiting for core's response...")
            result = self._result_queue.get()
            return result

        @app.get("/config", response_model=ConfigDTO)
        async def get_config(options: Optional[str] = ""):
            logger.info("get-config requests on api")
            return map_config(extract_config(), options)

        @app.put("/config")
        async def update_config(config: ConfigDTO):
            config_dict = map_to_config_file_format(config)

            success = update_and_restart_config(config_dict)
            return handle_config_response(config_dict, success)

        @app.get("/areas")
        async def list_areas():
            return {
                "areas": get_areas()
            }

        @app.get("/areas/{area_id}")
        async def get_area(area_id):
            area = next((area for area in get_areas() if area['id'] == area_id), None)
            if not area:
                raise HTTPException(status_code=404, detail=f'The area: {area_id} does not exist')
            return area

        @app.post('/areas')
        async def create_area(new_area: AreaConfigDTO):
            config_dict = extract_config()
            areas_name = [x for x in config_dict.keys() if x.startswith("Area")]
            areas = [map_area(x, config_dict) for x in areas_name]
            if new_area.id in [area['id'] for area in areas]:
                raise HTTPException(status_code=400, detail="Area already exists")

            cameras = [x for x in config_dict.keys() if x.startswith("Source")]
            cameras = [map_camera(x, config_dict, []) for x in cameras]
            camera_ids = [camera['id'] for camera in cameras]
            if not all(x in camera_ids for x in new_area.cameras.split(',')):
                non_existent_cameras = set(new_area.cameras.split(',')) - set(camera_ids)
                raise HTTPException(status_code=404, detail=f'The cameras: {non_existent_cameras} do not exist')

            config_dict[f'Area_{len(areas)}'] = map_to_area_file_format(new_area)

            success = update_and_restart_config(config_dict)
            return handle_config_response(config_dict, success)

        @app.put('/areas/{area_id}')
        async def edit_area(area_id, edited_area: AreaConfigDTO):
            edited_area.id = area_id
            config_dict = extract_config()
            area_names = [x for x in config_dict.keys() if x.startswith("Area")]
            areas = [map_area(x, config_dict) for x in area_names]
            areas_ids = [area['id'] for area in areas]
            try:
                index = areas_ids.index(area_id)
            except ValueError:
                raise HTTPException(status_code=404, detail=f'The area: {area_id} does not exist')

            cameras = [x for x in config_dict.keys() if x.startswith("Source")]
            cameras = [map_camera(x, config_dict, []) for x in cameras]
            camera_ids = [camera['id'] for camera in cameras]
            if not all(x in camera_ids for x in edited_area.cameras.split(',')):
                non_existent_cameras = set(edited_area.cameras.split(',')) - set(camera_ids)
                raise HTTPException(status_code=404, detail=f'The cameras: {non_existent_cameras} do not exist')

            config_dict[f"Area_{index}"] = map_to_area_file_format(edited_area)

            success = update_and_restart_config(config_dict)
            return handle_config_response(config_dict, success)

        @app.delete('/areas/{area_id}')
        async def delete_area(area_id):
            config_dict = extract_config()
            areas_name = [x for x in config_dict.keys() if x.startswith("Area")]
            areas = [map_area(x, config_dict) for x in areas_name]
            areas_ids = [area['id'] for area in areas]
            try:
                index = areas_ids.index(area_id)
            except ValueError:
                raise HTTPException(status_code=404, detail=f'The area: {area_id} does not exist')

            config_dict.pop(f'Area_{index}')
            config_dict = reestructure_areas((config_dict))

            success = update_and_restart_config(config_dict)
            return handle_config_response(config_dict, success)

        @app.get("/cameras")
        async def list_cameras(options: Optional[str] = ""):
            return {
                "cameras": get_cameras(options)
            }

        @app.get("/cameras/{camera_id}")
        async def get_camera(camera_id):
            camera = next((camera for camera in get_cameras(['withImage']) if camera['id'] == camera_id), None)
            if not camera:
                raise HTTPException(status_code=404, detail=f'The camera: {camera_id} does not exist')
            return camera

        @app.post("/cameras")
        async def create_camera(new_camera: SourceConfigDTO):
            config_dict = extract_config()
            cameras_name = [x for x in config_dict.keys() if x.startswith("Source")]
            cameras = [map_camera(x, config_dict, []) for x in cameras_name]
            if new_camera.id in [camera['id'] for camera in cameras]:
                raise HTTPException(status_code=400, detail="Camera already exists")

            config_dict[f'Source_{len(cameras)}'] = map_to_camera_file_format(new_camera)

            success = update_and_restart_config(config_dict)
            return handle_config_response(config_dict, success)

        @app.put("/cameras/{camera_id}")
        async def edit_camera(camera_id, edited_camera: SourceConfigDTO):
            edited_camera.id = camera_id
            config_dict = extract_config()
            camera_names = [x for x in config_dict.keys() if x.startswith("Source")]
            cameras = [map_camera(x, config_dict, []) for x in camera_names]
            cameras_ids = [camera['id'] for camera in cameras]
            try:
                index = cameras_ids.index(camera_id)
            except ValueError:
                raise HTTPException(status_code=404, detail=f'The camera: {camera_id} does not exist')

            config_dict[f"Source_{index}"] = map_to_camera_file_format(edited_camera)

            success = update_and_restart_config(config_dict)
            return handle_config_response(config_dict, success)

        @app.delete("/cameras/{camera_id}")
        async def delete_camera(camera_id):
            config_dict = extract_config()
            camera_names = [x for x in config_dict.keys() if x.startswith("Source")]
            cameras = [map_area(x, config_dict) for x in camera_names]
            cameras_ids = [camera['id'] for camera in cameras]
            try:
                index = cameras_ids.index(camera_id)
            except ValueError:
                raise HTTPException(status_code=404, detail=f'The camera: {camera_id} does not exist')

            config_dict = delete_camera_from_areas(camera_id, config_dict)

            config_dict.pop(f'Source_{index}')
            config_dict = reestructure_cameras((config_dict))

            success = update_and_restart_config(config_dict)
            return handle_config_response(config_dict, success)

        @app.get("/cameras/{camera_id}/image", response_model=ImageModel)
        async def get_camera_image(camera_id):
            dir_path = verify_path(self.config.get_section_dict("App")["ScreenshotsDirectory"], camera_id)
            with open(f'{dir_path}/default.jpg', "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read())
            return {
                "image": encoded_string
            }

        @app.put("/cameras/{camera_id}/image")
        async def replace_camera_image(camera_id, body: ImageModel):
            dir_path = verify_path(self.config.get_section_dict("App")["ScreenshotsDirectory"], camera_id)
            try:
                decoded_image = base64.b64decode(body.image.split(',')[1])
                nparr = np.fromstring(decoded_image, np.uint8)
                cv_image = cv.imdecode(nparr, cv.IMREAD_COLOR)
                cv.imwrite(f"{dir_path}/default.jpg", cv_image)
            except Exception:
                return HTTPException(status_code=400, detail="Invalid image format")

        @app.post("/cameras/{camera_id}/homography_matrix")
        async def config_calibrated_distance(camera_id, body: ConfigHomographyMatrix):
            dir_source = next((source for source in self.config.get_video_sources() if source['id'] == camera_id), None)
            if not dir_source:
                raise HTTPException(status_code=404, detail=f'The camera: {camera_id} does not exist')
            dir_path = get_camera_calibration_path(self.config, camera_id)
            compute_and_save_inv_homography_matrix(points=body, destination=dir_path)
            sections = self.config.get_sections()
            config_dict = {}
            for section in sections:
                config_dict[section] = self.config.get_section_dict(section)
            config_dict[dir_source['section']]['DistMethod'] = 'CalibratedDistance'

            success = update_and_restart_config(config_dict)
            return handle_config_response(config_dict, success)

        @app.get("/slack/is-enabled")
        def is_slack_enabled():
            return {
                "enabled": is_slack_configured()
            }

        @app.delete("/slack/revoke")
        def revoke_slack():
            write_user_token("")

        @app.post("/slack/add-channel")
        def add_slack_channel(channel: str):
            add_slack_channel_to_config(channel)

        @app.post("/slack/enable")
        def enable(body: SlackConfig):
            enable_slack(body)

        return app

    def start(self):
        kwargs = {
            "host": self._host,
            "port": self._port,
            "log_level": "info",
            "access_log": False,
        }
        if self.config.get_boolean("API", "SSLEnabled"):
            # HTTPs is enabled
            kwargs.update({
                "ssl_keyfile": f"{self.config.get_section_dict('API')['SSLKeyFile']}",
                "ssl_certfile": f"{self.config.get_section_dict('API')['SSLCertificateFile']}"
            })
        uvicorn.run(self.app, **kwargs)
