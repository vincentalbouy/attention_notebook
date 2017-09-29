import os
import sys
import argparse
import collections
import json
import re
from datetime import datetime

import numpy as np
import tensorflow as tf
from lstm import *
from tensorflow.contrib.tensorboard.plugins import projector
from tensorflow.contrib.lookup import index_to_string_table_from_file

id_to_w = {}
id_to_ans = {}
# img_dir = '/Users/larrychen/google-drive/GitHub/machine-intelligence/CLEVR/training_data/train_subset_000/images/'
img_dir = 'CLEVR_v1.0/images/train/'
# question_dir = '/Users/larrychen/Downloads/CLEVR_v1.0/questions/CLEVR_train_questions.json'
question_dir = 'CLEVR_v1.0/questions/CLEVR_train_questions.json'
pretrained = {}

date = datetime.now()
date_string = date.strftime('%Y-%m-%d-%H-%M-%S')

def build_embeddings(embedding_file, vocab_file):
    model = json.load(open(embedding_file))
    vocabulary = [word.strip() for word in open(vocab_file)]

    embedding_dim = len(model[vocabulary[1]][0])

    embedding = np.zeros((len(vocabulary), embedding_dim))
    dictionary = {'<PAD>': 0}
    reverse_dictionary = {0: '<PAD>'}
    for i, word in enumerate(vocabulary[1:]):
        vec = np.asarray(model[word]).reshape(1, embedding_dim)
        embedding[i + 1] = vec
        reverse_dictionary[i + 1] = word
        dictionary[word] = i + 1
    return embedding, dictionary, reverse_dictionary


def extract_pretrained_weights(checkpoint_file):
    from tensorflow.python import pywrap_tensorflow
    reader = pywrap_tensorflow.NewCheckpointReader(checkpoint_file)
    tensors = reader.get_variable_to_shape_map().keys()
    for t in tensors:
        info_dict = re.search(
            r'((block(?P<block>\d))/(unit_(?P<unit>\d+))/bottleneck_v1/)?(?P<branch>conv\d+|shortcut)/(BatchNorm/)?(?P<tensor>weights|gamma|beta|moving_mean|moving_variance)',
            t)
        if info_dict is None:
            continue
        else:
            info_dict = info_dict.groupdict()
        if info_dict['block'] is None:
            layer = 'res1'
            block = ''
            branch = ''
            conv = ''
        else:
            layer = 'res%d' % (int(info_dict['block']) + 1)
            block = 'block%d' % int(info_dict['unit'])
            branch = 'branch1' if info_dict['branch'] == 'shortcut' else 'branch2'
            conv = 0 if info_dict['branch'] == 'shortcut' else int(info_dict['branch'][-1])
            conv = '' if conv == 0 else str(chr(96 + conv))
        tensor = 'weights' if info_dict['tensor'] == 'weights' else 'batch_normalization/' + info_dict['tensor']
        name_scope = os.path.join(layer, block, branch, conv, tensor)
        pretrained[name_scope] = reader.get_tensor(t)
    pretrained['fully_connected/weights'] = reader.get_tensor('resnet_v1_101/logits/weights')
    pretrained['fully_connected/biases'] = reader.get_tensor('resnet_v1_101/logits/biases')


def serialize_examples(question_file, dictionary):
    def update_progress(current, total, task):
        bar_length = 50
        text = ''
        progress = float(current) / total
        num_dots = int(round(bar_length * progress))
        num_spaces = bar_length - num_dots
        if current == total:
            text = '\r\nDone.\r\n'
        else:
            text = ('\r[{}] {:.2f}% ' + task + ' {} of {}').format('.' * num_dots + ' ' * num_spaces, progress * 100,
                                                                   current, total)
        sys.stdout.write(text)
        sys.stdout.flush()

    print('Generating tfrecord file from %s' % question_file)
    data = json.load(open(question_file))

    questions = []
    answers = []
    image_files = []
    num_questions = len(data['questions'])
    for i, entry in enumerate(data['questions']):
        questions.append(re.split(r'\s+', re.sub(r'([;?])', r'', entry['question'])))
        answers.append(entry['answer'].lower())
        image_files.append(str.encode(entry['image_filename']))
        if i % 100 == 0:
            update_progress(i, num_questions, 'Parsing example')
    update_progress(num_questions, num_questions, 'Parsing example')

    print('Creating answer dictionary...', end=' ')
    examples = [[dictionary[word.lower()] for word in q] for q in questions]
    labels = []
    answer_dict = {}
    for a in answers:
        if a not in answer_dict:
            answer_dict[a] = len(answer_dict)
        labels.append(answer_dict[a])

    reverse_answer_dict = dict(zip(answer_dict.values(), answer_dict.keys()))
    with open(os.path.join('data', 'answers.json'), 'w') as fp:
        json.dump(reverse_answer_dict, fp)

    assert len(labels) == len(examples), "Num examples does not match num labels"
    print('Done.')

    def make_example(question, answer, image_file):
        ex = tf.train.SequenceExample()
        ex.context.feature['length'].int64_list.value.append(len(question))
        ex.context.feature['answer'].int64_list.value.append(answer_dict[answer])
        ex.context.feature['image_file'].bytes_list.value.append(image_file)
        fl_tokens = ex.feature_lists.feature_list['words']
        for token in question:
            fl_tokens.feature.add().int64_list.value.append(dictionary[token.lower()])
        return ex

    writer = tf.python_io.TFRecordWriter(os.path.join('data', 'train_examples.tfrecords'))
    for i, (question, answer, image_file) in enumerate(zip(questions, answers, image_files)):
        ex = make_example(question, answer, image_file)
        writer.write(ex.SerializeToString())
        if i % 100 == 0:
            update_progress(i, num_questions, 'Serializing example')
    update_progress(num_questions, num_questions, 'Serializing example')
    writer.close()


def generate_batch(_inputs, batch_size):
    with tf.name_scope('Batch_Generator'):
        return tf.train.batch(_inputs, batch_size, num_threads=1, capacity=10768, dynamic_pad=True)


def preprocess_example(_inputs):
    length, question, answer, image_file = _inputs

    with tf.name_scope('Extract_Image'):
        full_img_path = tf.string_join([tf.constant(img_dir, dtype=tf.string), image_file])
        img_data = tf.read_file(full_img_path)
        img = tf.image.decode_png(img_data, channels=3)

    # min_after_dequeue = 10000
    # dtypes = [tf.int64, tf.int64, tf.int64, tf.float32]
    # names = ['length', 'question', 'answer', 'image']
    # example_queue = tf.RandomShuffleQueue(capacity=min_after_dequeue+3*256, min_after_dequeue=min_after_dequeue, dtypes=dtypes, names=names, name='preprocess_queue')
    # enqueue_op = example_queue.enqueue({'length': length,
    #                                     'question': question,
    #                                     'answer': answer,
    #                                     'image': tf.cast(img, tf.float32)})
    # ex_qr = tf.train.QueueRunner(example_queue, [enqueue_op])
    # tf.train.add_queue_runner(ex_qr)
    # example = example_queue.dequeue()
    # example['length'].set_shape(_inputs[0].get_shape())
    # example['question'].set_shape(_inputs[1].get_shape())
    # example['answer'].set_shape(_inputs[2].get_shape())
    # example['image'].set_shape([224, 224, 3])
    # return [example['length'], example['question'], example['answer'], example['image']]
    return [length, question, answer, img]


def shuffle_examples(_inputs):
    with tf.variable_scope('shuffle'):
        dtypes = list(map(lambda x: x.dtype, _inputs))
        shapes = list(map(lambda x: x.get_shape(), _inputs))
        shuffle_queue = tf.RandomShuffleQueue(capacity=5768, min_after_dequeue=5000, dtypes=dtypes)
        enqueue_op = shuffle_queue.enqueue(_inputs)
        qr = tf.train.QueueRunner(shuffle_queue, [enqueue_op] * 4)
        tf.add_to_collection(tf.GraphKeys.QUEUE_RUNNERS, qr)
        out = shuffle_queue.dequeue()
        for tensor, shape in zip(out, shapes):
            tensor.set_shape(shape)
        return out


def input_pipeline(input_files, batch_size):
    with tf.name_scope('Input'):
        filename_queue = tf.train.string_input_producer(input_files, num_epochs=1000)
        reader = tf.TFRecordReader()
        _, serialized_example = reader.read(filename_queue)
        context_features = {
            'length': tf.FixedLenFeature([], dtype=tf.int64),
            'answer': tf.FixedLenFeature([], dtype=tf.int64),
            'image_file': tf.FixedLenFeature([], dtype=tf.string)
        }
        sequence_features = {
            'words': tf.FixedLenSequenceFeature([], dtype=tf.int64)
        }
        context_parsed, sequence_parsed = tf.parse_single_sequence_example(serialized_example, context_features,
                                                                           sequence_features)
        length = context_parsed['length']
        question = sequence_parsed['words']
        answer = context_parsed['answer']
        image_file = context_parsed['image_file']

        # min_after_dequeue = 10000
        # names= ['length', 'question', 'answer', 'image_file']
        # dtypes = [tf.int64, tf.int64, tf.int64, tf.string]
        # shuffle = tf.RandomShuffleQueue(capacity=min_after_dequeue+3*batch_size, min_after_dequeue=min_after_dequeue, dtypes=dtypes, names=names)
        # enqueue_op = shuffle.enqueue({'length': length,
        #                               'question': question,
        #                               'answer': answer,
        #                               'image_file': image_file})
        # shuffle_qr = tf.train.QueueRunner(shuffle, [enqueue_op])
        # tf.train.add_queue_runner(shuffle_qr)
        # example = shuffle.dequeue()
        # example['length'].set_shape(length.get_shape())
        # example['question'].set_shape(question.get_shape())
        # example['answer'].set_shape(answer.get_shape())
        # example['image_file'].set_shape(image_file.get_shape())

        # length = example['length']
        # question = example['question']
        # answer = example['answer']
        # image_file = example['image_file']

        example = preprocess_example((length, question, answer, image_file))
        #example = shuffle_examples(example)

        lengths, questions, answers, images = generate_batch(example, batch_size=batch_size)
        resized_images = tf.image.resize_images(images, [224, 224])

        tf.summary.image('Original_Image', images)
        #tf.summary.image('Resized_Image', resized_images)

        return lengths, questions, answers, resized_images
        # print(image_file)

        # full_img_path = tf.string_join([tf.constant(img_dir, dtype=tf.string), image_file])

        # img_data = tf.read_file(full_img_path)

        # img = tf.image.decode_png(img_data, channels=3)

        # img_batch = tf.reshape(img, [-1, 320, 480, 3])

        # # tf.summary.image('Input_Picture', img_batch)

        # resized_batch = tf.image.resize_images(img_batch, [224, 224])
        # # tf.summary.image('Resized', resized_batch)

        # return length, question, answer, resized_batch


def conv_layer(_input, ksize, out_channels, stride):
    name_scope = tf.contrib.framework.get_name_scope()
    initializer = tf.constant_initializer(pretrained[os.path.join(name_scope, 'weights')])
    kernel_shape = [ksize, ksize, _input.get_shape()[-1], out_channels]
    weights = tf.get_variable('weights', kernel_shape, initializer=initializer, trainable=False)
    return tf.nn.conv2d(_input, weights, strides=[1, stride, stride, 1], padding='SAME', name='conv')


def batch_norm(_input):
    name_scope = tf.contrib.framework.get_name_scope()
    init_gamma = tf.constant_initializer(pretrained[os.path.join(name_scope, 'batch_normalization', 'gamma')])
    init_beta = tf.constant_initializer(pretrained[os.path.join(name_scope, 'batch_normalization', 'beta')])
    init_mean = tf.constant_initializer(pretrained[os.path.join(name_scope, 'batch_normalization', 'moving_mean')])
    init_var = tf.constant_initializer(pretrained[os.path.join(name_scope, 'batch_normalization', 'moving_variance')])
    return tf.layers.batch_normalization(_input, beta_initializer=init_beta, gamma_initializer=init_gamma,
                                         moving_mean_initializer=init_mean, moving_variance_initializer=init_var,
                                         training=True, trainable=False)


def fc_layer(_input, out_channels):
    name_scope = tf.contrib.framework.get_name_scope()
    in_channels = _input.get_shape()[-1]
    _input = tf.reshape(_input, [-1, int(in_channels)])
    init_weights = tf.constant_initializer(pretrained[os.path.join(name_scope, 'weights')])
    init_biases = tf.constant_initializer(pretrained[os.path.join(name_scope, 'biases')])
    weights = tf.get_variable('weights', [in_channels, out_channels], initializer=init_weights, trainable=True)
    biases = tf.get_variable('biases', shape=[out_channels], initializer=init_biases, trainable=True)
    return tf.nn.xw_plus_b(_input, weights, biases)


def block(_input, out_channels, stride):
    with tf.variable_scope('branch2'):
        with tf.variable_scope('a'):
            out = conv_layer(_input, ksize=1, out_channels=out_channels, stride=stride)
            out = batch_norm(out)
            out = tf.nn.relu(out, name='relu')

        with tf.variable_scope('b'):
            out = conv_layer(out, ksize=3, out_channels=out_channels, stride=1)
            out = batch_norm(out)
            out = tf.nn.relu(out, name='relu')

        with tf.variable_scope('c'):
            out = conv_layer(out, ksize=1, out_channels=4 * out_channels, stride=1)
            out = batch_norm(out)

    with tf.variable_scope('branch1'):
        if stride != 1 or tf.contrib.framework.get_name_scope() == 'res2/block1/branch1':
            shortcut = conv_layer(_input, ksize=1, out_channels=4 * out_channels, stride=stride)
            shortcut = batch_norm(shortcut)
        else:
            shortcut = _input

    return tf.nn.relu(shortcut + out, name='relu')


def res_layer(_input, num_blocks, out_channels, stride):
    out = _input
    for i in range(num_blocks):
        with tf.variable_scope('block%d' % (i + 1)):
            stride = stride if i == 0 else 1
            out = block(out, out_channels, stride)
    return out


def res_net(_input):
  with tf.variable_scope('res1'):
    out = conv_layer(_input, ksize=7, out_channels=64, stride=2)
    out = batch_norm(out)
    out = tf.nn.max_pool(out, ksize=[1, 3, 3, 1], strides=[1, 2, 2, 1], padding='SAME')

  with tf.variable_scope('res2'):
    out = res_layer(out, num_blocks=3, out_channels=64, stride=1)

  with tf.variable_scope('res3'):
    out = res_layer(out, num_blocks=4, out_channels=128, stride=2)

 # with tf.variable_scope('res4'):
   # out = res_layer(out, num_blocks=23, out_channels=256, stride=2)
    #tf.summary.image("image_1" ,out[:,:,:,:3],3)


 #we cut out resnet5 to feat the 14X14 output in the stacked attention paper

#with tf.variable_scope('res5'):
     #out = res_layer(out, num_blocks=3, out_channels=512, stride=2)

    print(out)
    print('out')

     #out = tf.nn.avg_pool(out, ksize=[1, 7, 7, 1], strides=[1, 1, 1, 1], padding='VALID')

  #with tf.variable_scope('fully_connected'):
     #out = fc_layer(out, out_channels=1000)
     #print(out)

  return out


def soft_attention(image, query):
    print(query)
    print('qyery')
    with tf.variable_scope("soft_attention"):

        i = tf.reshape(image, [32, 28*28, 512], name=None)

        tf.summary.image("image_input", image[:, :, :, :3], max_outputs=1)

        with tf.variable_scope("ha"):
            weights_q = tf.get_variable('weights_q', [512, 512], initializer=tf.random_normal_initializer(), trainable=True)
            biases_q = tf.get_variable('biases_q', [512], initializer=tf.zeros_initializer())
            h1 = tf.nn.xw_plus_b(query, weights_q, biases_q)

            weights_i = tf.get_variable('weights_i', [512, 512],
                                      initializer=tf.random_normal_initializer(),
                                      trainable=True)
            a = tf.reshape(i, [-1, 512])
            h2 = tf.matmul(a, weights_i)
            h2 = tf.reshape(h2, [-1, 28*28, 512])

            h2 = tf.transpose(h2, [1, 0, 2])
            h = tf.add(h1, h2)
            h = tf.transpose(h, [1, 0, 2])

            ha = tf.tanh(h)

        with tf.variable_scope("pi"):
            weights = tf.get_variable('weights', [512, 1], initializer=tf.random_normal_initializer(), trainable=True)
            biases = tf.get_variable('biases', [784], initializer=tf.zeros_initializer())
            ha = tf.reshape(ha, [-1, 512])
            pi = tf.matmul(ha, weights)
            print("pi", pi)
            pi = tf.reshape(pi, [32, 784])
            pi = tf.add(pi, biases)
            pi = tf.nn.softmax(pi)
            # pi = tf.nn.xw_plus_b(ha, weights_attention_p, biases_attention_p)
            # pi = pi - tf.reduce_min(pi)
            # pi = tf.divide(tf.transpose(pi), tf.reduce_max(pi, -1))
            # pi = tf.transpose(pi)

        pi = tf.reshape(pi, [32, 28, 28], name=None)

        pii = tf.reshape(pi, [32, 28, 28, 1], name=None)
        tf.summary.image("atention_mask", pii, max_outputs=1)
        image = tf.transpose(image, [3, 0, 1, 2])
        output_image = tf.multiply(pi, image)
        image = tf.transpose(image, [1, 2, 3, 0])
        output_image = tf.transpose(output_image, [1, 2, 3, 0])
        tf.summary.image("image_output", output_image[:, :, :, :3], max_outputs=1)

        vi = tf.reduce_sum(tf.reshape(output_image, [32, 784, 512]), axis=1)
        u = vi + query

    return u, pii

    ################################################################


def mlp_layer(input1, input2):
    print(input1.get_shape())
    print(input2.get_shape())

    # A single fully connected layer just for the image part
    with tf.variable_scope('mlp'):
        with tf.variable_scope('h0'):
            weights = tf.get_variable('weights', [input1.get_shape()[1], 1024], initializer=tf.random_normal_initializer())
            biases = tf.get_variable('biases', [1024], initializer=tf.zeros_initializer())
            out = tf.nn.relu(tf.nn.xw_plus_b(input1, weights, biases))
            dim=out.get_shape()
            dim2=input2.get_shape()
            print(dim)
            print(dim2)

            # out = tf.nn.dropout(out, keep_prob=0.4)

        out = tf.concat([out, input2], axis=1)
        dim3 = out.get_shape()
        #print(dim3')
        print(dim3)

        with tf.variable_scope('h1'):
            weights = tf.get_variable('weights', [out.get_shape()[1], 1024], initializer=tf.random_normal_initializer())
            biases = tf.get_variable('biases', [1024], initializer=tf.zeros_initializer())
            out = tf.nn.relu(tf.nn.xw_plus_b(out, weights, biases))
            # out = tf.nn.dropout(out, keep_prob=0.4)
        with tf.variable_scope('out'):
            weights = tf.get_variable('weights', [out.get_shape()[1], 28], initializer=tf.random_normal_initializer())
            biases = tf.get_variable('biases', [28], initializer=tf.zeros_initializer())
            return tf.nn.xw_plus_b(out, weights, biases)


def train_ops(_input, labels):
    with tf.variable_scope('training'):
        loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(logits=_input, labels=labels))
        with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):
            train_op = tf.train.AdamOptimizer(5e-5).minimize(loss)
        tf.summary.scalar('cross_entropy', loss)

    for var in tf.trainable_variables():
        tf.summary.histogram(var.op.name, var)
    return train_op


def test_imagenet(image_dir):
    id_to_label = {}
    with open(os.path.join(image_dir, 'id_to_human.json')) as file:
        id_to_label = json.load(file)
    extract_pretrained_weights('data/resnet_v1_101.ckpt')
    img_files = tf.train.match_filenames_once(os.path.join(image_dir, '*.jpg'))
    filename_queue = tf.train.string_input_producer(img_files, shuffle=False)
    reader = tf.WholeFileReader()
    filename, image_file = reader.read(filename_queue)
    image = tf.image.decode_jpeg(image_file, channels=3)
    resized_image = tf.image.resize_images(image, [224, 224])
    filebatch, image_batch = tf.train.batch([filename, resized_image], batch_size=5, num_threads=1, capacity=64)

    tf.summary.image('image', image_batch)

    logits = res_net(image_batch)

    predictions = tf.nn.top_k(tf.nn.softmax(logits), k=8)

    init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
    with tf.Session() as sess:
        sess.run(init_op)
        summary_writer = tf.summary.FileWriter('pretrained_test', graph=sess.graph)
        summary_op = tf.summary.merge_all()
        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coord)
        summary, ps, fb = sess.run([summary_op, predictions, filebatch])
        summary_writer.add_summary(summary, 0)
        # print(fb)
        # print(ps)
        for i, top_k in enumerate(ps[1]):
            print('Predictions for %s: ' % fb[i])
            for p in top_k:
                print(id_to_label[str(p)])
            print('-' * 80)
        coord.request_stop()
        coord.join(threads)

def main(embedding):
    # img_files = tf.gfile.ListDirectory(img_dir)
    # print(img_files)
    # img_files = [os.path.join(img_dir, filename) for filename in img_files]
    # filename_queue = tf.train.string_input_producer(img_files, shuffle=False, num_epochs=1)
    # # img_reader = tf.WholeFileReader()
    # # name, img_data = img_reader.read(filename_queue)
    # name = filename_queue.dequeue()

    id_to_label = {}
    with open(os.path.join('data', 'id_to_human.json')) as file:
        id_to_label = json.load(file)

    extract_pretrained_weights('data/resnet_v1_101.ckpt')
    example_batch = input_pipeline(['data/train_examples.tfrecords'], batch_size=1)
    lengths, questions, answers, images = example_batch
    print('questions')
    print(questions)

    # apply resnet  until resnet4
    logits = res_net(images)
    print(logits)
    print('1')

    # image_cat = tf.argmax(tf.nn.softmax(logits), axis=1)

    last_hidden = lstm(questions, lengths, embedding)
    print('last hidden')
    print(last_hidden)


    # print(logits)
    # print(last_hidden)


    in_channels = logits.get_shape()[-1]
    print(in_channels)

    print(logits)
    query, pii = soft_attention(logits, last_hidden)

    # Apply upscaled attention mask to input image for visualization
    # This is just to help with visualization, it is not equivalent to reversed convolutions kind of thing (TODO?)
    sa_mask = pii * 255
    upscaled_sa_mask = tf.image.resize_area(sa_mask, [224, 224])

    images_graysacale = tf.image.rgb_to_grayscale(images)
    images_graysacale = tf.image.grayscale_to_rgb(images_graysacale)
    images_graysacale = (images_graysacale // 2) + 127

    image_r, image_g, image_b = tf.split(images_graysacale, num_or_size_splits=3, axis=3)
    image_g = tf.subtract(image_g, upscaled_sa_mask)
    image_b = tf.subtract(image_b, upscaled_sa_mask)

    image_masked = tf.stack([image_r, image_g, image_b])
    image_masked = tf.reshape(image_masked, [3, 32, 224, 224])
    image_masked = tf.transpose(image_masked, [1, 2, 3, 0])

    tf.summary.image("masked_input", image_masked, max_outputs=3)

    logits = tf.reshape(logits, [32, -1])

    with tf.variable_scope('answer'):
        weights = tf.get_variable('weights', [512, 28], initializer=tf.random_normal_initializer(), trainable=True)
        biases = tf.get_variable('biases', [28], initializer=tf.zeros_initializer, trainable=True)
        output = tf.nn.softmax(tf.nn.xw_plus_b(query, weights, biases))

    print('output')
    print(output)

    with tf.name_scope('inference'):
        predictions = tf.argmax(tf.nn.softmax(output), axis=1)
        correct = tf.equal(predictions, answers)
        accuracy = tf.reduce_mean(tf.cast(correct, tf.float32))
        tf.summary.scalar('accuracy', accuracy)
    # print(predictions)

    # print(tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES))
    # print(tf.get_collection(tf.GraphKeys.UPDATE_OPS))

    train_op = train_ops(output, answers)

    with tf.name_scope('Text_Decode'):
        question_table = index_to_string_table_from_file(vocabulary_file='data/vocabulary.txt', name='Question_Table')
        answer_table = index_to_string_table_from_file(vocabulary_file='data/answers.txt', name='Answer_Table')
        question_strings = tf.expand_dims(
            tf.reduce_join(question_table.lookup(tf.slice(questions, [0, 0], [31, -1])), axis=1, separator=' '), axis=1)
        answer_strings = tf.expand_dims(answer_table.lookup(tf.slice(answers, [0], [31])), axis=1)
        prediction_strings = tf.expand_dims(answer_table.lookup(tf.slice(predictions, [0], [31])), axis=1)
        labels = tf.constant(['Question', 'Answer', 'Prediction'], shape=[1, 3])
        qa_table = tf.concat([question_strings, answer_strings, prediction_strings], axis=1)
        qa_table = tf.concat([labels, qa_table], axis=0)
        print(qa_table)
        # qa_string = tf.string_join([qa_string, prediction_strings], separator='\r\nPredicted: ')
        tf.summary.text('Question', qa_table)

    init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
    saver = tf.train.Saver()

    with tf.Session() as sess:
        tf.tables_initializer().run()
        sess.run(init_op)

        summary_writer = tf.summary.FileWriter('logs/' + date_string, graph=sess.graph)
        summary_op = tf.summary.merge_all()

        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coord)

        for i in range(100):
            if i % 10 == 0:
                _, summary, acc = sess.run([train_op, summary_op, accuracy])
                summary_writer.add_summary(summary, i)
                print('\rStep %d, Accuracy: %f' % (i, acc))
            else:
                sess.run(train_op)
                print('\rStep %d' % i, end=' ' * 20)
            if i % 5000 == 0:
                saver.save(sess, 'logs/' + date_string + '/model.ckpt', global_step=i)

        coord.request_stop()
        coord.join(threads)


if __name__ == '__main__':
    embedding, dictionary, reverse_dictionary = build_embeddings('data/embeddings.json', 'data/vocab.tsv')
    # serialize_examples(question_dir, dictionary)
    main(embedding)
    # test_imagenet('/Users/larrychen/Downloads/images')
    # run_lstm(embedding)
