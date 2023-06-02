import boto3
import time
import imageio
import cv2
import numpy as np
from boto3.dynamodb.conditions import Key, Attr
import tensorflow as tf
from tensorflow import keras

C_TABLE = "jolpvideoclass"
IMG_SIZE = 224
MAX_SEQ_LENGTH = 50
NUM_FEATURES = 2048

def id_duplication_check(uid, TABLE):
	dynamo = boto3.resource('dynamodb')
	dtable = dynamo.Table(TABLE)
	response = dtable.scan(
		FilterExpression=Attr('id').eq(uid)
	)['Items']
	if response:
		return True
	else:
		return False

def register_member(uid, pw, TABLE):
	dynamo = boto3.resource('dynamodb')
	dtable = dynamo.Table(TABLE)
	dtable.put_item(Item = {
		'id':uid,
		'pw':pw
	})

def login_check(uid, pw, TABLE):
	dynamo = boto3.resource('dynamodb')
	dtable = dynamo.Table(TABLE)
	response = dtable.scan(
		FilterExpression=Attr('pw').eq(pw) & Attr('id').eq(uid)
	)['Items']
	if response:
		return True
	else:
		return False

def upload_video(file_name, uid, BUCKET, TABLE):
	s3 = boto3.client('s3')
	s3.upload_file(file_name, BUCKET, file_name)
	url = f"https://s3.ap-northeast-2.amazonaws.com/{BUCKET}/{file_name}"
	upload_time = time.strftime('%Y-%m-%d %X', time.localtime(time.time()))

	frames, class_name = sequence_prediction(file_name)
	gif_file = to_gif(frames[:], file_name)
	gif_file_name = file_name[:-4]+".gif"
	s3.upload_file(gif_file, BUCKET, gif_file_name)
	gif_url = f"https://s3.ap-northeast-2.amazonaws.com/{BUCKET}/{gif_file_name}"

	dynamo = boto3.resource('dynamodb')
	dtable = dynamo.Table(TABLE)
	dtable.put_item(Item = {
		'file_name':file_name,
		'id':uid,
		'file_url':gif_url,
		'class_name':class_name,
		'upload_time':upload_time
	})

def get_class_info(class_name, TABLE):
	dynamo = boto3.resource('dynamodb')
	dtable = dynamo.Table(TABLE)
	response = dtable.scan(
		FilterExpression=Attr('class_name').eq(class_name)
	)['Items']
	A_ratio = response[0]['A_ratio']
	B_ratio = response[0]['B_ratio']
	explanation = response[0]['explanation']
	ratio = A_ratio+" : "+B_ratio

	return ratio, explanation

def get_result(file_name, uid, TABLE):
	dynamo = boto3.resource('dynamodb')
	dtable = dynamo.Table(TABLE)
	response = dtable.scan(
		FilterExpression=Attr('file_name').eq(file_name) & Attr('id').eq(uid)
	)['Items']
	url = response[0]['file_url']
	class_name = response[0]['class_name']

	ratio, explanation = get_class_info(class_name, C_TABLE)
	
	return url, class_name, ratio, explanation



def get_all_video(uid, TABLE):
	dynamo = boto3.resource('dynamodb')
	dtable = dynamo.Table(TABLE)
	response = dtable.scan(
		FilterExpression= Attr('id').eq(uid)
	)['Items']
	if response:
		file_names = dtable.scan(
			FilterExpression=Attr('id').eq(uid), 
			ProjectionExpression='file_name'
		)['Items']

		for i in range(len(file_names)):
			file_names[i] = file_names[i]['file_name']
			file_names[i] = file_names[i][8:]

		class_names = dtable.scan(
			FilterExpression=Attr('id').eq(uid), 
			ProjectionExpression='class_name'
		)['Items']

		for i in range(len(class_names)):
			class_names[i] = class_names[i]['class_name']

		urls = dtable.scan(
			FilterExpression=Attr('id').eq(uid), 
			ProjectionExpression='file_url'
		)['Items']

		for i in range(len(urls)):
			urls[i] = urls[i]['file_url']

		upload_times = dtable.scan(
			FilterExpression=Attr('id').eq(uid), 
			ProjectionExpression='upload_time'
		)['Items']

		for i in range(len(upload_times)):
			upload_times[i] = upload_times[i]['upload_time']
		
	else:
		file_names = []
		urls = []
		class_names = []
		upload_times = []

	ratios = []
	for class_name in class_names:
		ratio, explanation = get_class_info(class_name, C_TABLE)
		ratios.append(ratio)

	return file_names, urls, class_names, upload_times, ratios


def delete_video(key, uid, BUCKET, TABLE):
	dynamo = boto3.resource('dynamodb')
	dtable = dynamo.Table(TABLE)
	file_name = "uploads/"+key
	
	response = dtable.delete_item(
		Key={
		'file_name': file_name,
		'id': uid
		}
	)
	s3 = boto3.client('s3') 
	s3.delete_object(Bucket=BUCKET, Key=file_name)


def admin_get_all_mem(TABLE):
	dynamo = boto3.resource('dynamodb')
	dtable = dynamo.Table(TABLE)
	response = dtable.scan()['Items']
	if response:
		uids = dtable.scan(
			ProjectionExpression='id'
		)['Items']

		for i in range(len(uids)):
			uids[i] = uids[i]['id']

	else:
		uids = []
	
	return uids

def crop_command(start, end, input, output):
	command = "ffmpeg -i " + input + " -ss " + start + " -to " + end + " " + output
	return command

def to_gif(frames, file_name):
	converted_frames = frames.astype(np.uint8)
	file_gif = file_name[:-4]+".gif"
	imageio.mimsave(file_gif, converted_frames, fps=8)
	return file_gif

def load_video(path, max_frames=0, resize=(IMG_SIZE, IMG_SIZE)):
	cap = cv2.VideoCapture(path)
	frames = []
	try:
		while True:
			ret, frame = cap.read()
			if not ret:
				break
			frame = cv2.resize(frame, resize)
			frame = frame[:, :, [2, 1, 0]]
			frames.append(frame)

			if len(frames) == max_frames:
				break
	finally:
		cap.release()
	return np.array(frames)


def prepare_single_video(frames):
	frames = frames[None, ...]
	frame_mask = np.zeros(shape=(1, MAX_SEQ_LENGTH,), dtype="bool")
	frame_features = np.zeros(shape=(1, MAX_SEQ_LENGTH, NUM_FEATURES), dtype="float32")
	feature_extractor = build_feature_extractor()
	for i, batch in enumerate(frames):
		video_length = batch.shape[0]
		length = min(MAX_SEQ_LENGTH, video_length)
		for j in range(length):
			frame_features[i, j, :] = feature_extractor.predict(batch[None, j, :])
			frame_mask[i, :length] = 1  # 1 = not masked, 0 = masked

	return frame_features, frame_mask


def sequence_prediction(file_name):
	class_vocab = ['1', '2', '11', '13', '21', '106', '110', '111']
	model = keras.models.load_model("1500_200_3type_saved_model.h5")
	frames = load_video(file_name)
	frame_features, frame_mask = prepare_single_video(frames)
	probabilities = model.predict([frame_features, frame_mask])[0]

	for i in np.argsort(probabilities)[::-1]:
		print(f"  {class_vocab[i]}: {probabilities[i] * 100:5.2f}%")
	class_name = class_vocab[probabilities.argmax(axis=-1)]
	class_probabilities = probabilities[probabilities.argmax(axis=-1)] * 100
	print(class_probabilities)
	return frames, class_name


def build_feature_extractor():
	feature_extractor = keras.applications.InceptionV3(
		weights="imagenet",
		include_top=False,
		pooling="avg",
		input_shape=(IMG_SIZE, IMG_SIZE, 3),
	)
	preprocess_input = keras.applications.inception_v3.preprocess_input

	inputs = keras.Input((IMG_SIZE, IMG_SIZE, 3))
	preprocessed = preprocess_input(inputs)

	outputs = feature_extractor(preprocessed)
	return keras.Model(inputs, outputs, name="feature_extractor")


