import cv2
import time
import os
import h5py

from keras.models import Model
from keras.layers import Input, concatenate, Conv2D, MaxPooling2D, Conv2DTranspose
from keras.layers import Activation, add, multiply, Lambda
from keras.layers import AveragePooling2D, average, UpSampling2D, Dropout
from keras.optimizers import Adam, SGD, RMSprop
from keras.initializers import glorot_normal, random_normal, random_uniform
from keras.callbacks import ModelCheckpoint, TensorBoard, EarlyStopping

from keras import backend as K
from keras.layers.normalization import BatchNormalization
from keras.models import load_model

import numpy as np
import tensorflow as tf

import matplotlib.pyplot as plt
from sklearn.metrics import roc_curve, auc, precision_recall_curve # roc curve tools
from sklearn.model_selection import train_test_split
from losses import *

kinit = 'glorot_normal'

def UnetConv2D(input, outdim, is_batchnorm, name):
	x = Conv2D(outdim, (3, 3), strides=(1, 1), kernel_initializer=kinit, padding="same", name=name+'_1')(input)
	if is_batchnorm:
		x =BatchNormalization(name=name + '_1_bn')(x)
	x = Activation('relu',name=name + '_1_act')(x)

	x = Conv2D(outdim, (3, 3), strides=(1, 1), kernel_initializer=kinit, padding="same", name=name+'_2')(x)
	if is_batchnorm:
		x = BatchNormalization(name=name + '_2_bn')(x)
	x = Activation('relu', name=name + '_2_act')(x)
	return x

def UnetGatingSignal(input, is_batchnorm, name):
    ''' this is simply 1x1 convolution, bn, activation '''
    shape = K.int_shape(input)
    x = Conv2D(shape[3] * 1, (1, 1), strides=(1, 1), padding="same",  kernel_initializer=kinit, name=name + '_conv')(input)
    if is_batchnorm:
        x = BatchNormalization(name=name + '_bn')(x)
    x = Activation('relu', name = name + '_act')(x)
    return x


def AttnGatingBlock(x, g, inter_shape, name):
    ''' take g which is the spatially smaller signal, do a conv to get the same
    number of feature channels as x (bigger spatially)
    do a conv on x to also get same geature channels (theta_x)
    then, upsample g to be same size as x
    add x and g (concat_xg)
    relu, 1x1 conv, then sigmoid then upsample the final - this gives us attn coefficients'''

    shape_x = K.int_shape(x)  # 32
    shape_g = K.int_shape(g)  # 16

    theta_x = Conv2D(inter_shape, (2, 2), strides=(2, 2), padding='same', name='xl'+name)(x)  # 16
    shape_theta_x = K.int_shape(theta_x)

    phi_g = Conv2D(inter_shape, (1, 1), padding='same')(g)
    upsample_g = Conv2DTranspose(inter_shape, (3, 3),strides=(shape_theta_x[1] // shape_g[1], shape_theta_x[2] // shape_g[2]),padding='same', name='g_up'+name)(phi_g)  # 16

    concat_xg = add([upsample_g, theta_x])
    act_xg = Activation('relu')(concat_xg)
    psi = Conv2D(1, (1, 1), padding='same', name='psi'+name)(act_xg)
    sigmoid_xg = Activation('sigmoid')(psi)
    shape_sigmoid = K.int_shape(sigmoid_xg)
    upsample_psi = UpSampling2D(size=(shape_x[1] // shape_sigmoid[1], shape_x[2] // shape_sigmoid[2]))(sigmoid_xg)  # 32

    upsample_psi = expend_as(upsample_psi, shape_x[3],  name)
    y = multiply([upsample_psi, x], name='q_attn'+name)

    result = Conv2D(shape_x[3], (1, 1), padding='same',name='q_attn_conv'+name)(y)
    result_bn = BatchNormalization(name='q_attn_bn'+name)(result)
    return result_bn

def expend_as(tensor, rep,name):
	my_repeat = Lambda(lambda x, repnum: K.repeat_elements(x, repnum, axis=3), arguments={'repnum': rep},  name='psi_up'+name)(tensor)
	return my_repeat


def attn_reg(img_size=512,start_neurons=32):
	print ("attention unet")
    # img_input = Input(shape=input_size, name='input_scale1')
	img_input = Input((img_size, img_size, 1),name='input_scale1')
	scale_img_2 = AveragePooling2D(pool_size=(2, 2), name='input_scale2')(img_input)
	scale_img_3 = AveragePooling2D(pool_size=(2, 2), name='input_scale3')(scale_img_2)
	scale_img_4 = AveragePooling2D(pool_size=(2, 2), name='input_scale4')(scale_img_3)

	conv1 = UnetConv2D(img_input, start_neurons, is_batchnorm=True, name='conv1')
	pool1 = MaxPooling2D(pool_size=(2, 2))(conv1)

	input2 = Conv2D(start_neurons*2, (3, 3), padding='same', activation='relu', name='conv_scale2')(scale_img_2)
	input2 = concatenate([input2, pool1], axis=3)
	conv2 = UnetConv2D(input2, start_neurons*2, is_batchnorm=True, name='conv2')
	pool2 = MaxPooling2D(pool_size=(2, 2))(conv2)
	input3 = Conv2D(start_neurons*4, (3, 3), padding='same', activation='relu', name='conv_scale3')(scale_img_3)
	input3 = concatenate([input3, pool2], axis=3)
	conv3 = UnetConv2D(input3, start_neurons*4, is_batchnorm=True, name='conv3')
	pool3 = MaxPooling2D(pool_size=(2, 2))(conv3)

	input4 = Conv2D(start_neurons*8, (3, 3), padding='same', activation='relu', name='conv_scale4')(scale_img_4)
	input4 = concatenate([input4, pool3], axis=3)
	conv4 = UnetConv2D(input4, start_neurons*2, is_batchnorm=True, name='conv4')
	pool4 = MaxPooling2D(pool_size=(2, 2))(conv4)

	center = UnetConv2D(pool4, start_neurons*16, is_batchnorm=True, name='center')

	g1 = UnetGatingSignal(center, is_batchnorm=True, name='g1')
	attn1 = AttnGatingBlock(conv4, g1, start_neurons*4, '_1')
	up1 = concatenate([Conv2DTranspose(32, (3,3), strides=(2,2), padding='same', activation='relu', kernel_initializer=kinit)(center), attn1], name='up1')

	g2 = UnetGatingSignal(up1, is_batchnorm=True, name='g2')
	attn2 = AttnGatingBlock(conv3, g2, start_neurons*2, '_2')
	up2 = concatenate([Conv2DTranspose(start_neurons*2, (3,3), strides=(2,2), padding='same', activation='relu', kernel_initializer=kinit)(up1), attn2], name='up2')

	g3 = UnetGatingSignal(up1, is_batchnorm=True, name='g3')
	attn3 = AttnGatingBlock(conv2, g3, start_neurons, '_3')
	up3 = concatenate([Conv2DTranspose(start_neurons, (3,3), strides=(2,2), padding='same', activation='relu', kernel_initializer=kinit)(up2), attn3], name='up3')

	up4 = concatenate([Conv2DTranspose(start_neurons, (3,3), strides=(2,2), padding='same', activation='relu', kernel_initializer=kinit)(up3), conv1], name='up4')

	conv6 = UnetConv2D(up1, start_neurons*8, is_batchnorm=True, name='conv6')
	conv7 = UnetConv2D(up2, start_neurons*4, is_batchnorm=True, name='conv7')
	conv8 = UnetConv2D(up3, start_neurons*2, is_batchnorm=True, name='conv8')
	conv9 = UnetConv2D(up4, start_neurons, is_batchnorm=True, name='conv9')

	out6 = Conv2D(1, (1, 1), activation='sigmoid', name='pred1')(conv6)
	out7 = Conv2D(1, (1, 1), activation='sigmoid', name='pred2')(conv7)
	out8 = Conv2D(1, (1, 1), activation='sigmoid', name='pred3')(conv8)
	out9 = Conv2D(1, (1, 1), activation='sigmoid', name='final')(conv9)

	model = Model(inputs=[img_input], outputs=[out6, out7, out8, out9])

	model.compile(optimizer=Adam(lr=1e-5), loss=dice_coef_loss, metrics=[dice_coef])
	return model
